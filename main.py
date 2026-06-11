from __future__ import annotations

import json
import logging
import os
import sys

# 尽早加载 secrets.env（含 CURSOR_API_KEY），供 exe 与 Cursor 桥接使用
from secrets_loader import load_secrets_env

load_secrets_env()
import threading
import time
import traceback
import atexit
import ctypes
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from broker_futu import FutuLiveBroker, FutuLiveBrokerConfig
from config import FUTU, STRATEGY, TRADE
from alpha_multifactor import load_symbol_list_json
from data_provider import fetch_daily_kline, get_last_price
from market_risk_overlay import RiskOverlayAdjustments, build_overlay
from gui_confirm import confirm_order_dialog
from daily_report import (
    build_daily_summary,
    format_daily_push_content,
    mark_daily_push_sent,
    should_send_daily_push_now,
    write_daily_report_csv,
)
from market_context import (
    classify_news_impact,
    extract_impact_titles,
    fetch_news_summary,
    format_news_title_lines,
    summarize_sell_context,
)
from holdings import (
    apply_buy,
    apply_sell,
    get_holding_any,
    has_holding,
    load_holdings,
    save_holdings,
    update_peak_price,
)
from indicators import add_indicators, latest_indicators
from notifier import PushPlusNotifier
from pending_orders import create_pending_order, expire_pending_orders, find_pending_match, load_pending_orders, save_pending_orders
from position_sizer import PositionConstraints, calc_buy_qty
from report import append_trade_record, load_trades
from risk_state import evaluate_buy_pause, is_buy_paused, load_risk_state, save_risk_state
from risk_manager import append_trade_log, build_buy_plan, build_sell_plan
from signal_cache import (
    has_pending_same_side,
    is_in_cooldown,
    load_signal_cache,
    mark_signal,
    save_signal_cache,
    update_submitted_orders_status,
)
from strategy import analyze, resolve_market_mode
from utils.logging_setup import setup_logging
from wechat_confirm import process_wechat_confirmations
from pushplus_confirm import process_pushplus_confirmations, start_clawbot_bridge_process, start_clawbot_listener
from secrets_loader import get_cursor_api_key
from windows_session_listener import start_windows_session_listener, write_shutdown_hook
from news_verdict_tracker import (
    log_verdict,
    should_send_weekly_review_now,
    mark_weekly_review_sent,
    build_weekly_verdict_push,
    evaluate_pending_outcomes,
)

WATCHLIST_PATH = os.path.join("config", "watchlist.json")
ALPHA_FACTOR_UNIVERSE_PATH = os.path.join("config", "alpha_factor_universe.json")
RUNTIME_SNAPSHOT_PATH = os.path.join("logs", "runtime_snapshot.json")
CORE_STATE_PATH = os.path.join("logs", "core_state.json")
PORTFOLIO_STATE_PATH = os.path.join("logs", "portfolio_state.json")
def _news_state_path() -> str:
    """Resolve news state path next to the EXE (or cwd for source runs)."""
    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(base, "logs", "news_monitor_state.json")

NEWS_STATE_PATH = _news_state_path()


def _extract_order_id(data) -> str | None:
    if isinstance(data, pd.DataFrame) and not data.empty and "order_id" in data.columns:
        try:
            return str(data.iloc[0]["order_id"])
        except Exception:
            return None
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_iso_to_local_str(iso_utc: str) -> str:
    try:
        dt_utc = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        tz_name = (getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip()
        return dt_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_utc


def _alpha_cycle_symbols() -> list[str]:
    """
    Merge watchlist + optional factor file + configured extensions, intersected with TRADE.alpha_tradable_codes.
    US.VOO (CORE) is excluded from Alpha scanning; CORE/DCA logic handles it separately.
    """
    wl = [str(s).strip().upper() for s in _load_watchlist_symbols() if str(s).strip()]
    factor_syms = load_symbol_list_json(ALPHA_FACTOR_UNIVERSE_PATH)
    allowed = TRADE.alpha_tradable_codes
    core_sym = str(TRADE.core_symbol).strip().upper()
    merged: set[str] = set()
    for s in wl + factor_syms + list(TRADE.alpha_extended_symbols):
        u = str(s).strip().upper()
        if u and u in allowed and u != core_sym:
            merged.add(u)
    return sorted(merged)


def _load_watchlist_symbols() -> list[str]:
    default = [str(x).strip().upper() for x in TRADE.symbols]
    try:
        os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
        if not os.path.exists(WATCHLIST_PATH):
            with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
                json.dump({"symbols": default, "updated_at": ""}, f, ensure_ascii=False, indent=2)
            return default

        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        syms = [str(x).strip().upper() for x in (data.get("symbols") or []) if str(x).strip()]
        return syms if syms else default
    except Exception:
        return default


def _save_runtime_snapshot(
    *,
    market_mode: str,
    account_drawdown_pct: float,
    candidate_ranking: str,
    portfolio_return_pct: float = 0.0,
    core_return_pct: float = 0.0,
    alpha_return_pct: float = 0.0,
    outperform_voo: bool = False,
    allocation_text: str = "",
) -> None:
    try:
        os.makedirs("logs", exist_ok=True)
        with open(RUNTIME_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "market_mode": market_mode,
                    "account_drawdown_pct": float(account_drawdown_pct),
                    "candidate_ranking": candidate_ranking,
                    "portfolio_return_pct": float(portfolio_return_pct),
                    "core_return_pct": float(core_return_pct),
                    "alpha_return_pct": float(alpha_return_pct),
                    "outperform_voo": bool(outperform_voo),
                    "allocation_text": allocation_text,
                    "updated_at": _now_iso(),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        append_trade_log({"action": "ERROR", "error": f"runtime_snapshot_write_failed: {e}"})


def _format_candidate_ranking(candidates: list[dict]) -> str:
    if not candidates:
        return "无候选"
    sorted_items = sorted(candidates, key=lambda x: (-int(x.get("score", 0)), str(x.get("code", ""))))
    top = sorted_items[:5]
    return ", ".join([f"{x['code']}:{int(x['score'])}" for x in top])


def _cash_pct_for_symbol(code: str) -> float:
    code_u = code.upper()
    if code_u in set(TRADE.core_etf_symbols):
        return float(STRATEGY.core_etf_cash_pct)
    if code_u in set(TRADE.high_vol_symbols):
        return float(STRATEGY.high_vol_cash_pct)
    return float(STRATEGY.quality_tech_cash_pct)


def _alpha_bucket(symbol: str) -> str:
    s = symbol.upper()
    if s in set(TRADE.high_vol_alpha_symbols):
        return "HIGH_VOL"
    if s in set(TRADE.trend_core_alpha_symbols):
        return "TREND_CORE"
    return "OTHER"


def _alpha_buy_cash_pct(symbol: str) -> float:
    bucket = _alpha_bucket(symbol)
    if bucket == "HIGH_VOL":
        return float(STRATEGY.alpha_high_vol_cash_pct)
    return float(STRATEGY.alpha_trend_core_cash_pct)


def _alpha_stop_loss_pct(symbol: str) -> float:
    bucket = _alpha_bucket(symbol)
    if bucket == "HIGH_VOL":
        return float(STRATEGY.alpha_high_vol_stop_loss_pct)
    return float(STRATEGY.alpha_trend_stop_loss_pct)


def _alpha_trailing_pct(symbol: str) -> float:
    bucket = _alpha_bucket(symbol)
    if bucket == "HIGH_VOL":
        return float(STRATEGY.alpha_high_vol_trailing_pct)
    return float(STRATEGY.alpha_trend_trailing_pct)


def _alpha_allow_add(symbol: str) -> bool:
    return _alpha_bucket(symbol) == "HIGH_VOL"


def _load_json(path: str, default: dict) -> dict:
    try:
        if not os.path.exists(path):
            return dict(default)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        out = dict(default)
        out.update(data)
        return out
    except Exception:
        return dict(default)


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _days_since(last_date_iso: str, today_date_str: str) -> int:
    if not last_date_iso:
        return 10**9
    try:
        # Accept both date-only "YYYY-MM-DD" and full ISO datetime strings.
        if "T" in last_date_iso:
            last_dt = datetime.fromisoformat(last_date_iso.replace("Z", "+00:00"))
            last_d = last_dt.date()
        else:
            last_d = datetime.strptime(str(last_date_iso).strip()[:10], "%Y-%m-%d").date()

        today_d = datetime.strptime(str(today_date_str).strip()[:10], "%Y-%m-%d").date()
        return int((today_d - last_d).days)
    except Exception:
        # If parsing fails, treat as long time ago to allow actions rather than crash.
        return 10**9


def _create_core_pending(
    *,
    pending_orders,
    cache_items,
    notifier: PushPlusNotifier,
    core_symbol: str,
    current_price: float,
    target_cash: float,
    available_cash: float,
    market_mode: str,
    account_drawdown_pct: float,
    label: str,
) -> None:
    append_trade_log(
        {
            "code": core_symbol,
            "action": "CORE_PENDING_CHECK",
            "message": f"{label}: current_price={current_price:.4f} target_cash={target_cash:.2f} available_cash={available_cash:.2f}",
        }
    )
    # Respect global safety cap for real trading single order.
    capped_cash = min(float(target_cash), float(available_cash), float(TRADE.max_order_usd))
    limit_price = float(current_price) * float(TRADE.limit_price_factor)
    qty = int(float(capped_cash) // float(limit_price)) if limit_price > 0 else 0
    if qty < 1:
        append_trade_log(
            {
                "code": core_symbol,
                "action": "CORE_BUY_BLOCKED",
                "message": f"{label}: qty<1 under cap/cash. target_cash={target_cash:.2f} capped_cash={capped_cash:.2f} limit={limit_price:.4f}",
            }
        )
        return
    _create_pending_and_notify(
        pending_orders=pending_orders,
        cache_items=cache_items,
        notifier=notifier,
        code=core_symbol,
        side="BUY",
        qty=qty,
        limit_price=limit_price,
        current_price=float(current_price),
        score=0,
        reason=(
            f"CORE {label}: target_cash={target_cash:.2f}, capped_cash={capped_cash:.2f}, "
            f"limit_price={limit_price:.4f}"
        ),
        holding_count=0,
        available_cash=available_cash,
        market_mode=market_mode,
        account_drawdown_pct=account_drawdown_pct,
        candidate_rank="CORE",
    )


def _allow_reentry(symbol: str, kdf: pd.DataFrame, holding_any, *, min_days_since_sell: int) -> bool:
    if holding_any is None or not str(getattr(holding_any, "last_sell_time", "")).strip():
        return True
    if "time_key" not in kdf.columns:
        return False
    df = kdf.copy()
    df["trade_date"] = pd.to_datetime(df["time_key"], errors="coerce")
    if len(df) < int(STRATEGY.reentry_breakout_lookback_days) + 1:
        return False
    last_sell_dt = pd.to_datetime(str(holding_any.last_sell_time), errors="coerce", utc=True)
    if pd.isna(last_sell_dt):
        return False
    last_sell_date = last_sell_dt.tz_convert("UTC").date()
    trading_days_since_sell = int((df["trade_date"].dt.date > last_sell_date).sum())
    if trading_days_since_sell < int(min_days_since_sell):
        return False
    recent_window = df.iloc[-(int(STRATEGY.reentry_breakout_lookback_days) + 1) : -1]
    if recent_window.empty:
        return False
    breakout_high = float(recent_window["close"].max())
    current_close = float(df.iloc[-1]["close"])
    return current_close > breakout_high


def _holding_days_on_kdf(buy_time_utc: str, kdf: pd.DataFrame) -> int:
    if not buy_time_utc or "time_key" not in kdf.columns:
        return 0
    buy_dt = pd.to_datetime(str(buy_time_utc), errors="coerce", utc=True)
    if pd.isna(buy_dt):
        return 0
    df = kdf.copy()
    df["trade_date"] = pd.to_datetime(df["time_key"], errors="coerce").dt.date
    buy_date = buy_dt.tz_convert("UTC").date()
    return int((df["trade_date"] >= buy_date).sum())


def _is_breakout_high(kdf: pd.DataFrame, *, lookback_days: int) -> bool:
    if "close" not in kdf.columns:
        return False
    n = int(max(lookback_days, 1))
    if len(kdf) < n + 1:
        return False
    prev = kdf.iloc[-(n + 1) : -1]
    if prev.empty:
        return False
    return float(kdf.iloc[-1]["close"]) > float(prev["close"].max())


def _is_choppy(kdf: pd.DataFrame, *, window_days: int, abs_ret_pct: float) -> bool:
    if "close" not in kdf.columns:
        return False
    n = int(max(window_days, 1))
    if len(kdf) < n + 1:
        return False
    c0 = float(kdf.iloc[-(n + 1)]["close"])
    c1 = float(kdf.iloc[-1]["close"])
    if c0 <= 0:
        return False
    ret = abs((c1 / c0 - 1.0) * 100.0)
    return ret > float(abs_ret_pct)


def _pct_above(value: float, base: float | None) -> float | None:
    try:
        if base is None or float(base) <= 0:
            return None
        return (float(value) / float(base) - 1.0) * 100.0
    except Exception:
        return None


def _buy_entry_quality_blocks(
    *,
    code: str,
    ind,
    bucket: str,
    rank_pct: float,
    relaxed_entry: bool,
) -> list[str]:
    """
    Final guardrail before a BUY enters the pending-order pool.
    It keeps the score model from chasing already-stretched moves.
    """
    blocks: list[str] = []
    bucket_u = str(bucket).upper()
    is_high_vol = bucket_u == "HIGH_VOL"

    max_ret5d = (
        float(STRATEGY.high_vol_buy_max_ret5d_pct)
        if is_high_vol
        else float(STRATEGY.buy_max_ret5d_pct)
    )
    if ind.ret5d_pct is not None and float(ind.ret5d_pct) > max_ret5d:
        blocks.append(f"ret5d too hot {float(ind.ret5d_pct):.2f}%>{max_ret5d:.2f}%")

    min_ret5d = float(STRATEGY.buy_min_ret5d_pct)
    if ind.ret5d_pct is not None and float(ind.ret5d_pct) < min_ret5d:
        blocks.append(f"ret5d falling too fast {float(ind.ret5d_pct):.2f}%<{min_ret5d:.2f}%")

    if ind.rsi14 is not None and float(ind.rsi14) > float(STRATEGY.buy_max_rsi):
        blocks.append(f"RSI overheat {float(ind.rsi14):.1f}>{float(STRATEGY.buy_max_rsi):.1f}")

    ma20_ext = _pct_above(float(ind.close), ind.ma20)
    ma20_cap = (
        float(STRATEGY.high_vol_buy_max_ma20_extension_pct)
        if is_high_vol
        else float(STRATEGY.buy_max_ma20_extension_pct)
    )
    if ma20_ext is not None and ma20_ext > ma20_cap:
        blocks.append(f"price extended vs MA20 {ma20_ext:.1f}%>{ma20_cap:.1f}%")

    ma60_ext = _pct_above(float(ind.close), ind.ma60)
    ma60_cap = (
        float(STRATEGY.high_vol_buy_max_ma60_extension_pct)
        if is_high_vol
        else float(STRATEGY.buy_max_ma60_extension_pct)
    )
    if ma60_ext is not None and ma60_ext > ma60_cap:
        blocks.append(f"price extended vs MA60 {ma60_ext:.1f}%>{ma60_cap:.1f}%")

    if relaxed_entry and float(rank_pct) < float(STRATEGY.buy_relaxed_min_rank_pct):
        blocks.append(
            f"relaxed entry rank too low {float(rank_pct) * 100.0:.1f}%"
            f"<{float(STRATEGY.buy_relaxed_min_rank_pct) * 100.0:.1f}%"
        )

    return blocks

def _reason_to_plain_text(reason: str) -> str:
    """
    Make strategy reasons more human-readable in notifications.
    Keep it lightweight: translate common keywords and soften technical wording.
    """
    r = (reason or "").strip()
    if not r:
        return ""

    # Normalize separators for Chinese readability.
    r = r.replace(";", "；").replace("|", "｜")

    # Common patterns.
    r = r.replace("MA20<MA60", "短期均线跌破中期均线（趋势转弱）")
    r = r.replace("MA20>MA60", "短期均线在中期均线之上（趋势偏强）")
    r = r.replace("MA200", "200日均线")
    r = r.replace("MA60", "60日均线")
    r = r.replace("MA20", "20日均线")

    # Strategy keywords.
    r = r.replace("STOP_LOSS", "触发止损")
    r = r.replace("TRAILING_STOP", "触发追踪止盈/止损")
    r = r.replace("TREND_BREAK", "趋势转弱")
    r = r.replace("TECH_SELL:", "技术面转弱：")
    r = r.replace("SELL signal:", "卖出信号：")
    r = r.replace("BUY signal:", "买入信号：")
    r = r.replace("HOLD_LOCK", "持仓保护期（不建议太快卖）")

    # Less jargon from analysis reason strings.
    r = r.replace("strong_exception", "强势白名单")
    r = r.replace("ignore_rsi", "忽略RSI限制")

    return r


def _format_notify_content(
    *,
    code: str,
    signal: str,
    current_price: float,
    limit_price: float,
    qty: int,
    score: int,
    reason: str,
    holding_count: int,
    available_cash: float,
    market_mode: str = "",
    account_drawdown_pct: float = 0.0,
    candidate_rank: str = "",
) -> str:
    tail = ""
    if market_mode:
        tail += f"\n市场模式: {market_mode}"
    tail += f"\n账户回撤: {account_drawdown_pct:.2f}%"
    if candidate_rank:
        tail += f"\n候选排名: {candidate_rank}"
    plain_reason = _reason_to_plain_text(reason)
    return (
        f"股票代码: {code}\n"
        f"信号: {signal}\n"
        f"当前价: {current_price:.4f}\n"
        f"限价: {limit_price:.4f}\n"
        f"数量: {qty}\n"
        f"评分: {score}\n"
        f"原因: {plain_reason or reason}\n"
        f"当前持仓数: {holding_count}\n"
        f"币种: USD\n"
        f"账户现金(USD): {available_cash:.2f}\n"
        "风险提示: 本系统不会自动下单，必须人工确认。"
        + tail
    )


def _load_news_state() -> dict:
    try:
        if os.path.exists(NEWS_STATE_PATH):
            with open(NEWS_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        return {}
    return {}


def _save_news_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(NEWS_STATE_PATH), exist_ok=True)
        with open(NEWS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _run_news_scan(notifier: PushPlusNotifier, symbols: list[str]) -> None:
    """Execute one news scan cycle. Called from the dedicated monitor thread.

    推送策略：
    - 持仓股票：无论排名如何，有新消息就推（持仓相关信息优先级最高）
    - 非持仓股票：只推打分 top N 只（TP_NEWS_TOP_NONHOLDING，默认3）
    """
    print(f"[NEWS] 开始舆情扫描 symbols={symbols}", flush=True)
    logging.info("[NEWS] 开始舆情扫描 symbols=%s", symbols)
    state = _load_news_state()
    sent: dict = state.get("sent", {}) if isinstance(state.get("sent"), dict) else {}

    # 读取当前持仓股票集合
    try:
        _holdings_now = load_holdings()
        held_symbols: set[str] = {str(h.symbol).strip().upper() for h in _holdings_now if int(h.qty) > 0}
    except Exception:
        held_symbols = set()

    # 读取 runtime snapshot 里的候选打分排名（格式："CODE1:分, CODE2:分, ..."）
    top_nonholding_n = int(os.getenv("TP_NEWS_TOP_NONHOLDING", "3"))
    try:
        snap = _load_json(RUNTIME_SNAPSHOT_PATH, {})
        ranking_str = str(snap.get("candidate_ranking", "") or "")
        top_scored: list[str] = []
        for part in ranking_str.split(","):
            part = part.strip()
            if ":" in part:
                top_scored.append(part.split(":")[0].strip().upper())
        top_scored = top_scored[:top_nonholding_n]
    except Exception:
        top_scored = []

    # 扫描顺序：持仓股 → top打分非持仓股 → 其余（但非持仓且不在top的不推）
    all_codes = [str(x).strip().upper() for x in (symbols or []) if str(x).strip()]
    allowed_nonholding = set(top_scored)
    # 按优先级重排：持仓在前，top非持仓次之，其余排最后（用于日志，不推送）
    ordered_codes = (
        [c for c in all_codes if c in held_symbols]
        + [c for c in all_codes if c not in held_symbols and c in allowed_nonholding]
    )

    pushed = 0
    nonholding_pushed = 0
    max_push = int(os.getenv("TP_NEWS_MONITOR_MAX_PUSH", "20"))
    for code in ordered_codes:
        if pushed >= max_push:
            break
        is_holding = code in held_symbols
        if not is_holding and nonholding_pushed >= top_nonholding_n:
            continue  # 非持仓且非持仓配额已满，跳过
        try:
            news = fetch_news_summary(code, max_items=int(os.getenv("TP_NEWS_RSS_MAX_ITEMS", "8")))
            if news is None:
                print(f"[NEWS] {code}: RSS返回空", flush=True)
                continue

            print(f"[NEWS] {code}: {len(news.titles)}条标题 bull={news.bullish_hits} bear={news.bearish_hits}", flush=True)

            prev = sent.get(code, [])
            if not isinstance(prev, list):
                prev = []

            # First try impact-filtered titles; fall back to ALL new titles so
            # we never miss headlines just because they lack impact keywords.
            impact_titles = extract_impact_titles(news)
            candidate_titles = impact_titles if impact_titles else news.titles
            new_titles = [t for t in candidate_titles if t not in prev]
            if not new_titles:
                print(f"[NEWS] {code}: 无新标题（已全部推送过）", flush=True)
                continue

            verdict, bull, bear = classify_news_impact(news)

            keep_n = int(os.getenv("TP_NEWS_MONITOR_KEEP_TITLES", "30"))
            sent[code] = (prev + new_titles)[-keep_n:]

            bull_s = "、".join(bull[:6]) if bull else "无"
            bear_s = "、".join(bear[:6]) if bear else "无"
            flag = "（重点事件）" if impact_titles else ""
            lines = [
                f"股票: {code}  初步判断: {verdict}{flag}",
                f"利好线索: {bull_s}",
                f"利空线索: {bear_s}",
                "最新头条:",
            ]
            lines.extend(format_news_title_lines(new_titles, max_items=5))
            content = "\n".join(lines)
            holding_tag = "【持仓】" if is_holding else f"【TOP{top_nonholding_n}候选】"
            ok, resp = notifier.send(title=f"📰 舆情{holding_tag} {code} {verdict}", content=content)
            pushed += 1
            if not is_holding:
                nonholding_pushed += 1
            print(f"[NEWS] {code}: 已推送 verdict={verdict} holding={is_holding} ok={ok}", flush=True)
            logging.info("[NEWS] %s: 已推送 verdict=%s holding=%s new_titles=%d ok=%s", code, verdict, is_holding, len(new_titles), ok)
            if ok and verdict in ("利好", "利空"):
                from news_verdict_tracker import _fetch_yahoo_price
                price_now = _fetch_yahoo_price(code) or 0.0
                log_verdict(code, verdict, price_now, bull_keywords=bull, bear_keywords=bear)
        except Exception as exc:
            print(f"[NEWS] {code}: 扫描异常 {exc}", flush=True)
            logging.warning("[NEWS] %s: 扫描异常 %s", code, exc)
            continue

    print(f"[NEWS] 扫描完成 pushed={pushed}", flush=True)
    logging.info("[NEWS] 扫描完成 pushed=%d", pushed)
    state["last_run_ts"] = int(time.time())
    state["sent"] = sent
    _save_news_state(state)


def _start_news_monitor_thread(notifier: PushPlusNotifier, symbols_fn) -> None:
    """
    Launch a daemon thread that independently scans news every hour.
    symbols_fn() is called each iteration to get the latest symbol list.
    """
    if os.getenv("TP_NEWS_MONITOR_ENABLED", "1").strip() in ("0", "false", "False", "no", "NO"):
        print("[NEWS] 舆情监控已禁用 (TP_NEWS_MONITOR_ENABLED=0)", flush=True)
        return

    interval_min = int(os.getenv("TP_NEWS_MONITOR_INTERVAL_MIN", "60"))

    def _loop() -> None:
        print(f"[NEWS] 监控线程启动，扫描间隔={interval_min}分钟", flush=True)
        logging.info("[NEWS] 监控线程启动，扫描间隔=%d分钟", interval_min)
        while True:
            try:
                state = _load_news_state()
                now_ts = int(time.time())
                last_ts = int(state.get("last_run_ts", 0) or 0)
                elapsed = now_ts - last_ts if last_ts else interval_min * 60 + 1
                if elapsed >= interval_min * 60:
                    syms = symbols_fn()
                    _run_news_scan(notifier, syms)
                else:
                    wait_left = interval_min * 60 - elapsed
                    print(f"[NEWS] 距下次扫描还有 {wait_left//60}分{wait_left%60}秒", flush=True)
            except Exception as exc:
                print(f"[NEWS] 监控线程异常: {exc}", flush=True)
                logging.warning("[NEWS] 监控线程异常: %s", exc)
            # Check every 5 minutes whether it's time to scan.
            time.sleep(300)

    t = threading.Thread(target=_loop, name="news-monitor", daemon=True)
    t.start()


def _maybe_hourly_news_monitor(*, notifier: PushPlusNotifier, symbols: list[str]) -> None:
    """Kept for backward compat; the real work is now done by the background thread."""
    pass


def _create_pending_and_notify(
    *,
    pending_orders,
    cache_items,
    notifier: PushPlusNotifier,
    code: str,
    side: str,
    qty: int,
    limit_price: float,
    current_price: float,
    score: int,
    reason: str,
    holding_count: int,
    available_cash: float,
    market_mode: str = "",
    account_drawdown_pct: float = 0.0,
    candidate_rank: str = "",
) -> None:
    side_u = side.upper()
    if has_pending_same_side(pending_orders, symbol=code, side=side_u):
        return
    if is_in_cooldown(cache_items, symbol=code, side=side_u, cooldown_seconds=STRATEGY.notify_cooldown_seconds):
        print(f"[COOLDOWN] skip notify {code} {side_u}, cooldown={STRATEGY.notify_cooldown_seconds}s")
        return

    po = create_pending_order(
        symbol=code,
        side=side_u,
        qty=qty,
        limit_price=limit_price,
        expires_in_seconds=STRATEGY.confirm_code_expire_seconds,
    )
    po.message = reason
    pending_orders.append(po)
    save_pending_orders(pending_orders)

    mark_signal(cache_items, symbol=code, side=side_u)
    save_signal_cache(cache_items)

    title = f"TradePilot {'BUY' if side_u == 'BUY' else 'SELL'} 信号待确认 {code}"
    est_amount = float(qty) * float(limit_price)
    content = (
        _format_notify_content(
            code=code,
            signal=side_u,
            current_price=current_price,
            limit_price=limit_price,
            qty=qty,
            score=score,
            reason=reason,
            holding_count=holding_count,
            available_cash=available_cash,
            market_mode=market_mode,
            account_drawdown_pct=account_drawdown_pct,
            candidate_rank=candidate_rank,
        )
        + "\n"
        + f"pending_order_id: {po.order_id}\n"
        + f"expire_time_local: {_utc_iso_to_local_str(po.expire_time)}\n"
        + f"order_side: {side_u}\n"
        + f"order_qty: {qty}\n"
        + f"order_limit_price: {limit_price:.4f}\n"
        + f"order_value_est_usd: {est_amount:.2f}\n"
        + f"confirm_code: {po.confirm_code}\n"
        + f"确认格式: YES {side_u} {po.symbol} {po.qty} {po.confirm_code}\n"
        + "（方案1-PushPlus确认：在PushPlus里“发送消息给自己”，标题填 TP_CONFIRM，内容可用两种格式：\n"
        + "  1) 粘贴上面整行 YES BUY/SELL ...\n"
        + "  2) 只发送 confirm_code（适合手机快速确认））"
    )
    ok, msg = notifier.send(title=title, content=content)
    print(f"[NOTIFY] {code} {side_u} ok={ok} msg={msg}")
    append_trade_log(
        {
            "code": code,
            "score": score,
            "selected": 0,
            "action": f"PENDING_{side_u}",
            "current_price": current_price,
            "limit_price": limit_price,
            "qty": qty,
            "est_amount": qty * limit_price,
            "market_mode": market_mode,
            "account_drawdown_pct": account_drawdown_pct,
            "candidate_rank": candidate_rank,
            "message": f"pending_order_id={po.order_id}; notify={msg}; reason={reason}",
        }
    )


def _apply_settled_pending_updates(pending_orders) -> None:
    """
    Apply holdings/trade-history side effects once when a pending order reaches EXECUTED.
    """
    changed = False
    holdings = load_holdings()
    core_sym = str(TRADE.core_symbol).strip().upper()
    for po in pending_orders:
        if po.status != "EXECUTED":
            continue
        if "[APPLIED]" in (po.message or ""):
            continue

        if po.side.upper() == "BUY":
            apply_buy(holdings, symbol=po.symbol, qty=int(po.qty), price=float(po.limit_price))
            # CORE 实际成交后才记录 last_buy_date，避免未买入也重置定投计时器
            if str(po.symbol).strip().upper() == core_sym:
                try:
                    core_st = _load_json(CORE_STATE_PATH, {})
                    today_local = datetime.now(ZoneInfo(
                        (getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip()
                    )).strftime("%Y-%m-%d")
                    core_st["last_buy_date"] = today_local
                    _save_json(CORE_STATE_PATH, core_st)
                except Exception:
                    pass
        elif po.side.upper() == "SELL":
            apply_sell(holdings, symbol=po.symbol, qty=int(po.qty))

        append_trade_record(
            {
                "ts_utc": _now_iso(),
                "symbol": po.symbol,
                "side": po.side.upper(),
                "qty": int(po.qty),
                "price": float(po.limit_price),
                "order_id": po.broker_order_id or "",
            }
        )
        po.message = (po.message or "") + " [APPLIED]"
        changed = True

    if changed:
        save_holdings(holdings)
        save_pending_orders(pending_orders)


def _confirm_and_execute_pending(broker: FutuLiveBroker, notifier: PushPlusNotifier) -> None:
    pending_orders = load_pending_orders()
    if expire_pending_orders(pending_orders):
        save_pending_orders(pending_orders)

    active = [x for x in pending_orders if x.status == "PENDING"]
    if not active:
        print("No pending order to confirm in this cycle.")
        return

    print("Pending orders:")
    for o in active:
        print(
            f"- id={o.order_id} {o.symbol} {o.side} qty={o.qty} "
            f"limit={o.limit_price:.4f} expire_local={_utc_iso_to_local_str(o.expire_time)} shown={o.prompt_shown}"
        )

    # If previous pending was already prompted and still unresolved, do not pop again.
    waiting = [x for x in active if bool(x.prompt_shown)]
    if waiting:
        print("Previous pending order is still awaiting manual decision; skip duplicate popup.")
        return

    # Only prompt one newest-unshown pending each cycle.
    unshown = [x for x in active if not bool(x.prompt_shown)]
    if not unshown:
        return
    po = sorted(unshown, key=lambda x: x.created_at)[0]
    po.prompt_shown = True
    po.updated_at = _now_iso()
    save_pending_orders(pending_orders)

    gui_res = confirm_order_dialog(
        order_id=po.order_id,
        symbol=po.symbol,
        side=po.side,
        qty=int(po.qty),
        price=float(po.limit_price),
        confirm_code=po.confirm_code,
    )

    if gui_res.action != "CONFIRM":
        po.status = "CANCELLED"
        po.updated_at = _now_iso()
        po.message = "Cancelled by GUI user action"
        save_pending_orders(pending_orders)
        append_trade_log(
            {
                "code": po.symbol,
                "selected": 1,
                "action": po.side,
                "qty": po.qty,
                "limit_price": po.limit_price,
                "message": "GUI_CANCELLED",
            }
        )
        return

    # Keep confirm_code logic as secondary check.
    po2 = find_pending_match(
        pending_orders,
        side=po.side,
        symbol=po.symbol,
        qty=int(po.qty),
        confirm_code=gui_res.entered_code,
    )
    if po2 is None:
        po.status = "FAILED"
        po.updated_at = _now_iso()
        po.message = "Secondary confirm_code check failed"
        save_pending_orders(pending_orders)
        append_trade_log(
            {
                "code": po.symbol,
                "selected": 1,
                "action": po.side,
                "qty": po.qty,
                "limit_price": po.limit_price,
                "order_ok": 0,
                "error": "confirm_code secondary check failed",
            }
        )
        return

    side = po.side.upper()
    current_price = get_last_price(broker.quote_ctx, po.symbol)
    est_amount = float(po.qty) * float(po.limit_price)
    if est_amount > float(TRADE.max_order_usd):
        raise RuntimeError(f"Blocked: single order amount ${est_amount:.2f} > max ${TRADE.max_order_usd:.2f}")
    if side == "BUY":
        available_cash = broker.get_available_cash()
        if available_cash < est_amount:
            raise RuntimeError(
                f"Blocked: insufficient cash for BUY. cash={available_cash:.2f} need={est_amount:.2f}"
            )
    else:
        held_qty = broker.get_position_qty(po.symbol)
        if held_qty < int(po.qty):
            raise RuntimeError(f"Blocked: insufficient position for SELL. held={held_qty} need={po.qty}")

    broker.ensure_us_stock_only(po.symbol)
    broker.ensure_regular_session(po.symbol)

    ok, data = broker.place_limit_order(
        code=po.symbol,
        side=side,
        qty=int(po.qty),
        price=float(po.limit_price),
    )
    order_id = _extract_order_id(data)
    status_msg = ""
    if order_id:
        try:
            qdf = broker.query_order(order_id)
            if qdf is not None and not qdf.empty:
                status_msg = str(qdf.iloc[0].get("order_status", ""))
        except Exception:
            status_msg = ""

    po.broker_order_id = order_id or ""
    po.broker_status = status_msg
    po.updated_at = _now_iso()
    if not ok:
        po.status = "FAILED"
    elif any(x in status_msg.upper() for x in ("FILLED", "DEALT")):
        po.status = "EXECUTED"
    else:
        po.status = "SUBMITTED"
    po.message = str(data)
    save_pending_orders(pending_orders)

    if po.status == "EXECUTED":
        _apply_settled_pending_updates(pending_orders)

    append_trade_log(
        {
            "code": po.symbol,
            "selected": 1,
            "action": side,
            "current_price": current_price,
            "limit_price": po.limit_price,
            "qty": po.qty,
            "est_amount": est_amount,
            "order_ok": 1 if ok else 0,
            "order_id": order_id or "",
            "order_status": status_msg,
            "message": str(data),
        }
    )

    # Human-friendly order result push (USD only).
    try:
        tz = ZoneInfo((getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip())
        now_local = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        now_local = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    holding_buy_price = None
    try:
        holdings_now = load_holdings()
        h = get_holding_any(holdings_now, po.symbol)
        if h is not None:
            holding_buy_price = float(getattr(h, "buy_price", 0) or 0) or None
    except Exception:
        holding_buy_price = None

    pnl_amt = None
    pnl_pct = None
    if side == "SELL" and holding_buy_price and holding_buy_price > 0:
        pnl_amt = (float(po.limit_price) - float(holding_buy_price)) * float(po.qty)
        pnl_pct = (float(po.limit_price) / float(holding_buy_price) - 1.0) * 100.0

    cash_after = None
    assets_after = None
    try:
        cash_after = float(broker.get_available_cash())
        assets_after = float(broker.get_total_assets())
    except Exception:
        cash_after = None
        assets_after = None

    action_cn = "买入" if side == "BUY" else "卖出"
    est_usd = float(po.qty) * float(po.limit_price)
    lines = [
        f"时间(本地): {now_local}",
        f"{action_cn}: {po.symbol}",
        f"数量: {int(po.qty)} 股",
        f"委托价: {float(po.limit_price):.4f} USD",
        f"预计金额: {est_usd:.2f} USD" + (" (买入)" if side == "BUY" else " (卖出)"),
    ]
    if side == "SELL":
        if holding_buy_price:
            lines.append(f"持仓成本: {holding_buy_price:.4f} USD")
        if pnl_amt is not None and pnl_pct is not None:
            sign = "+" if pnl_amt >= 0 else ""
            lines.append(f"预计盈亏: {sign}{pnl_amt:.2f} USD ({sign}{pnl_pct:.2f}%)")
        else:
            lines.append("预计盈亏: N/A")

    if cash_after is not None:
        lines.append(f"余额现金(USD): {cash_after:.2f}")
    if assets_after is not None:
        lines.append(f"总资产(USD): {assets_after:.2f}")

    lines.extend(
        [
            f"下单结果: {'成功' if ok else '失败'}",
            f"订单号: {order_id or ''}",
            f"状态: {status_msg}",
        ]
    )

    notifier.send(
        title=f"TradePilot 订单结果 {action_cn} {po.symbol}",
        content="\n".join(lines),
    )


def _run_once(broker: FutuLiveBroker, notifier: PushPlusNotifier) -> None:
    symbols = _alpha_cycle_symbols()
    pending_orders = load_pending_orders()
    if expire_pending_orders(pending_orders):
        save_pending_orders(pending_orders)
    update_submitted_orders_status(pending_orders, broker)
    _apply_settled_pending_updates(pending_orders)
    cache_items = load_signal_cache()
    holdings = load_holdings()
    risk_state = load_risk_state(equity_base_usd=STRATEGY.drawdown_equity_base_usd)
    risk_state = evaluate_buy_pause(
        trades=load_trades(),
        state=risk_state,
        equity_base_usd=STRATEGY.drawdown_equity_base_usd,
        max_drawdown_pct=STRATEGY.max_account_drawdown_pct,
        max_consecutive_loss=STRATEGY.max_consecutive_loss,
        pause_days=STRATEGY.pause_new_buy_days,
    )
    save_risk_state(risk_state)

    try:
        available_cash = broker.get_available_cash()
        total_assets = broker.get_total_assets()
    except Exception:
        available_cash = 0.0
        total_assets = 0.0

    try:
        pos_df = broker.get_positions()
        pos_map = {}
        if pos_df is not None and not pos_df.empty:
            for _, row in pos_df.iterrows():
                code = str(row.get("code", "")).upper()
                try:
                    pos_map[code] = int(float(row.get("qty", 0)))
                except Exception:
                    pos_map[code] = 0
        else:
            pos_map = {}
    except Exception as e:
        pos_map = {}
        append_trade_log({"action": "ERROR", "error": f"position_snapshot_failed: {e}"})

    core_symbol = str(TRADE.core_symbol).upper()
    core_qty = int(pos_map.get(core_symbol, 0))
    silence_core_push = bool(getattr(STRATEGY, "core_silence_push_until_holding", False)) and core_qty <= 0
    core_state = _load_json(
        CORE_STATE_PATH,
        {
            "last_suggest_date": "",
            "last_buy_date": "",
            "recent_high": 0.0,
            "dip_triggered_5": False,
            "dip_triggered_10": False,
            "paused_trend": False,
            "paused_crash": False,
        },
    )
    today_local = datetime.now(ZoneInfo((getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip())).strftime(
        "%Y-%m-%d"
    )

    # 1) Market regime filter (SPY/VOO)
    regime_symbol = TRADE.regime_symbol
    try:
        regime_kdf = fetch_daily_kline(broker.quote_ctx, regime_symbol, days=700)
    except Exception:
        regime_symbol = TRADE.benchmark_symbol
        regime_kdf = fetch_daily_kline(broker.quote_ctx, regime_symbol, days=700)
    regime_ind = latest_indicators(add_indicators(regime_kdf))
    market_mode = resolve_market_mode(regime_ind)
    buy_paused = is_buy_paused(risk_state)
    spy_dd_pct = 0.0
    try:
        spy_df = add_indicators(regime_kdf)
        if not spy_df.empty and "close" in spy_df.columns:
            spy_close = float(spy_df.iloc[-1]["close"])
            spy_52w_high = float(spy_df["close"].tail(252).max())
            if spy_52w_high > 0:
                spy_dd_pct = (spy_52w_high - spy_close) / spy_52w_high * 100.0
    except Exception:
        spy_dd_pct = 0.0
    trend_pause = market_mode != "ATTACK"
    crash_pause = spy_dd_pct >= float(STRATEGY.spy_drawdown_pause_pct)
    if (not silence_core_push) and trend_pause and not bool(core_state.get("paused_trend", False)):
        notifier.send(title="CORE风险提示", content=f"{TRADE.spy_symbol} 跌破 MA200，暂停 CORE 买入")
    if (not silence_core_push) and crash_pause and not bool(core_state.get("paused_crash", False)):
        notifier.send(
            title="CORE风险提示",
            content=f"{TRADE.spy_symbol} 距52周高点回撤 {spy_dd_pct:.2f}% >= {STRATEGY.spy_drawdown_pause_pct:.1f}% ，暂停 CORE 加仓",
        )
    core_state["paused_trend"] = bool(trend_pause)
    core_state["paused_crash"] = bool(crash_pause)
    append_trade_log(
        {
            "action": "MARKET_MODE",
            "code": regime_symbol,
            "market_mode": market_mode,
            "account_drawdown_pct": risk_state.drawdown_pct,
            "message": (
                f"close={regime_ind.close:.4f}; ma200={regime_ind.ma200}; spy_dd_52w={spy_dd_pct:.2f}%; "
                f"buy_paused={buy_paused}; pause_until={risk_state.pause_buy_until}"
            ),
        }
    )

    # CORE module: DCA + Dip Buy (signal only)
    core_price = get_last_price(broker.quote_ctx, core_symbol)
    core_value = float(core_qty) * float(core_price)
    core_target_value = float(total_assets) * float(STRATEGY.core_ratio)
    try:
        core_kdf = fetch_daily_kline(broker.quote_ctx, core_symbol, days=800)
        core_kdf = add_indicators(core_kdf)
        recent_high = float(core_state.get("recent_high", 0.0) or 0.0)
        if not core_kdf.empty and "close" in core_kdf.columns:
            rolling_high = float(core_kdf["close"].tail(252).max())
            if rolling_high > recent_high:
                core_state["recent_high"] = rolling_high
                core_state["dip_triggered_5"] = False
                core_state["dip_triggered_10"] = False
            recent_high = float(core_state.get("recent_high", rolling_high))
        else:
            recent_high = float(core_state.get("recent_high", core_price))
    except Exception:
        core_kdf = pd.DataFrame()
        recent_high = float(core_state.get("recent_high", core_price))

    if core_qty <= 0 and core_state.get("last_suggest_date") != today_local and (not silence_core_push):
        # If no CORE holding, create a pending order suggestion so user can confirm via PushPlus.
        core_msg = (
            f"CORE 建议买入 {core_symbol}：当前没有CORE持仓（仅提示，不自动下单）\n"
            "为什么现在提示：\n"
            f"- 这是CORE的初始建仓提醒（目标占比约 {float(STRATEGY.core_ratio) * 100.0:.0f}%）\n"
            f"- 当前市场模式：{market_mode}\n"
            f"- 风控开关：趋势暂停={bool(trend_pause)}，回撤暂停={bool(crash_pause)}"
        )
        append_trade_log({"code": core_symbol, "action": "CORE_SUGGEST", "message": core_msg})
        ok, resp = notifier.send(title="CORE买入信号", content=core_msg)
        append_trade_log({"code": core_symbol, "action": "CORE_SUGGEST_PUSH", "message": f"ok={ok} resp={resp}"})
        # Only create pending when market allows CORE buys (respect pauses).
        if (core_value < core_target_value) and (not trend_pause) and (not crash_pause):
            init_cash = core_target_value / float(max(int(STRATEGY.core_dca_chunks), 1))
            _create_core_pending(
                pending_orders=pending_orders,
                cache_items=cache_items,
                notifier=notifier,
                core_symbol=core_symbol,
                current_price=core_price,
                target_cash=init_cash,
                available_cash=available_cash,
                market_mode=market_mode,
                account_drawdown_pct=risk_state.drawdown_pct,
                label="INIT(no holding)",
            )
        # Only mark as suggested if push succeeded; otherwise retry next cycle.
        if ok:
            core_state["last_suggest_date"] = today_local

    if core_value < core_target_value and (not trend_pause) and (not crash_pause):
        # Defensive: never allow None to participate in numeric comparisons.
        days_since_dca = _days_since(str(core_state.get("last_buy_date", "")), today_local)
        if days_since_dca is None:
            days_since_dca = 10**9
        interval_days = STRATEGY.core_dca_interval_days
        interval_i = int(interval_days) if interval_days is not None else 0
        if int(days_since_dca) >= interval_i:
            if not silence_core_push:
                chunk_cash = core_target_value / float(max(int(STRATEGY.core_dca_chunks), 1))
                core_ratio_pct = float(STRATEGY.core_ratio) * 100.0
                current_ratio_pct = (core_value / float(total_assets) * 100.0) if float(total_assets) > 0 else 0.0
                core_msg = (
                    f"VOO 分批建仓建议: symbol={core_symbol} price={core_price:.4f} chunk_cash={chunk_cash:.2f} "
                    f"target={core_target_value:.2f}\n"
                    "为什么现在提示：\n"
                    f"- 距离上次定投约 {int(days_since_dca)} 天，已达到设置的间隔 {int(interval_i)} 天\n"
                    f"- 当前CORE市值 {core_value:.2f}（占总资产 {current_ratio_pct:.1f}%）还没到目标 {core_target_value:.2f}（目标占比 {core_ratio_pct:.0f}%）\n"
                    f"- 本次建议买入金额：{chunk_cash:.2f}（目标金额÷{int(STRATEGY.core_dca_chunks)}份）\n"
                    f"- 当前市场模式：{market_mode}"
                )
                append_trade_log({"code": core_symbol, "action": "CORE_DCA_SIGNAL", "message": core_msg})
                ok, resp = notifier.send(title="CORE买入信号", content=core_msg)
                append_trade_log({"code": core_symbol, "action": "CORE_DCA_PUSH", "message": f"ok={ok} resp={resp}"})
                _create_core_pending(
                    pending_orders=pending_orders,
                    cache_items=cache_items,
                    notifier=notifier,
                    core_symbol=core_symbol,
                    current_price=core_price,
                    target_cash=chunk_cash,
                    available_cash=available_cash,
                    market_mode=market_mode,
                    account_drawdown_pct=risk_state.drawdown_pct,
                    label="DCA",
                )
            # last_buy_date 仅在实际成交时记录（由 _apply_settled_pending_updates 处理）

        if recent_high > 0:
            pullback = (core_price / recent_high) - 1.0
            if (pullback <= float(STRATEGY.core_dip_level_1)) and (not bool(core_state.get("dip_triggered_5", False))):
                if not silence_core_push:
                    base_cash = core_target_value / float(max(int(STRATEGY.core_dca_chunks), 1))
                    cash = base_cash * float(STRATEGY.core_dip_multiplier_1)
                    current_ratio_pct = (core_value / float(total_assets) * 100.0) if float(total_assets) > 0 else 0.0
                    core_ratio_pct = float(STRATEGY.core_ratio) * 100.0
                    msg = (
                        f"VOO 回调买入建议(-5%): price={core_price:.4f} recent_high={recent_high:.4f} amount={cash:.2f}\n"
                        "为什么现在提示：\n"
                        f"- 价格从近期高点回撤约 {(pullback * 100.0):.2f}%（达到设置阈值 "
                        f"{float(STRATEGY.core_dip_level_1) * 100.0:.0f}%）\n"
                        f"- 当前CORE市值 {core_value:.2f}（占总资产 {current_ratio_pct:.1f}% / 目标 {core_ratio_pct:.0f}%）\n"
                        f"- 本次加仓金额：{cash:.2f}（基础份额×乘数 {float(STRATEGY.core_dip_multiplier_1):.2f}）\n"
                        f"- 当前市场模式：{market_mode}"
                    )
                    append_trade_log({"code": core_symbol, "action": "CORE_DIP_SIGNAL", "message": msg})
                    ok, resp = notifier.send(title="CORE加仓信号", content=msg)
                    append_trade_log({"code": core_symbol, "action": "CORE_DIP_PUSH", "message": f"ok={ok} resp={resp}"})
                    _create_core_pending(
                        pending_orders=pending_orders,
                        cache_items=cache_items,
                        notifier=notifier,
                        core_symbol=core_symbol,
                        current_price=core_price,
                        target_cash=cash,
                        available_cash=available_cash,
                        market_mode=market_mode,
                        account_drawdown_pct=risk_state.drawdown_pct,
                        label="DIP(-5%)",
                    )
                    core_state["dip_triggered_5"] = True
            if (pullback <= float(STRATEGY.core_dip_level_2)) and (not bool(core_state.get("dip_triggered_10", False))):
                if not silence_core_push:
                    base_cash = core_target_value / float(max(int(STRATEGY.core_dca_chunks), 1))
                    cash = base_cash * float(STRATEGY.core_dip_multiplier_2)
                    current_ratio_pct = (core_value / float(total_assets) * 100.0) if float(total_assets) > 0 else 0.0
                    core_ratio_pct = float(STRATEGY.core_ratio) * 100.0
                    msg = (
                        f"VOO 回调买入建议(-10%): price={core_price:.4f} recent_high={recent_high:.4f} amount={cash:.2f}\n"
                        "为什么现在提示：\n"
                        f"- 价格从近期高点回撤约 {(pullback * 100.0):.2f}%（达到设置阈值 "
                        f"{float(STRATEGY.core_dip_level_2) * 100.0:.0f}%）\n"
                        f"- 当前CORE市值 {core_value:.2f}（占总资产 {current_ratio_pct:.1f}% / 目标 {core_ratio_pct:.0f}%）\n"
                        f"- 本次加仓金额：{cash:.2f}（基础份额×乘数 {float(STRATEGY.core_dip_multiplier_2):.2f}）\n"
                        f"- 当前市场模式：{market_mode}"
                    )
                    append_trade_log({"code": core_symbol, "action": "CORE_DIP_SIGNAL", "message": msg})
                    ok, resp = notifier.send(title="CORE加仓信号", content=msg)
                    append_trade_log({"code": core_symbol, "action": "CORE_DIP_PUSH", "message": f"ok={ok} resp={resp}"})
                    _create_core_pending(
                        pending_orders=pending_orders,
                        cache_items=cache_items,
                        notifier=notifier,
                        core_symbol=core_symbol,
                        current_price=core_price,
                        target_cash=cash,
                        available_cash=available_cash,
                        market_mode=market_mode,
                        account_drawdown_pct=risk_state.drawdown_pct,
                        label="DIP(-10%)",
                    )
                    core_state["dip_triggered_10"] = True

    _save_json(CORE_STATE_PATH, core_state)

    # 2) Holdings exit checks: stop-loss / MA60 / trailing-stop
    holdings_changed = False
    for h in holdings:
        if int(h.qty) <= 0:
            continue
        code = h.symbol.upper()
        if code == core_symbol and (not bool(getattr(STRATEGY, "core_exit_enabled", True))):
            append_trade_log({"code": code, "action": "CORE_HOLD", "message": "CORE asset held; exit disabled"})
            continue
        try:
            current = get_last_price(broker.quote_ctx, code)
            old_peak = float(h.highest_price_since_entry or 0.0)
            old_armed = bool(h.trailing_armed)
            update_peak_price(
                holdings,
                symbol=code,
                current_price=current,
                trailing_activate_pct=STRATEGY.trailing_activate_pct,
            )
            if float(h.highest_price_since_entry or 0.0) != old_peak or bool(h.trailing_armed) != old_armed:
                holdings_changed = True
            pnl_pct = ((current - float(h.buy_price)) / float(h.buy_price) * 100.0) if h.buy_price > 0 else 0.0
            reason = ""
            kdf = fetch_daily_kline(broker.quote_ctx, code, days=700)
            ind = latest_indicators(add_indicators(kdf))
            ar = analyze(
                code,
                ind,
                buy_threshold=STRATEGY.buy_score_threshold,
                market_ok=(market_mode == "ATTACK"),
                rank_top=True,
                market_mode=market_mode,
                strong_exception=(code in set(TRADE.strong_exception_symbols)),
                rsi_upper=STRATEGY.strong_rsi_upper,
                ignore_rsi=(code in set(TRADE.no_rsi_limit_symbols)),
            )
            # Split risk rules:
            # - CORE (VOO): conservative exit rules (separate thresholds)
            # - HIGH_VOL (e.g., HUT): aggressive thresholds already handled by alpha_* params
            if code == core_symbol:
                stop_loss = pnl_pct <= -float(getattr(STRATEGY, "core_stop_loss_pct", 15.0))
            else:
                stop_loss = pnl_pct <= -float(_alpha_stop_loss_pct(code))
            trail_hit = bool(h.trailing_armed) and float(h.highest_price_since_entry or 0.0) > 0 and (
                current
                <= float(h.highest_price_since_entry)
                * (
                    1.0
                    - float(
                        getattr(STRATEGY, "core_trailing_drawdown_pct", _alpha_trailing_pct(code))
                        if code == core_symbol
                        else _alpha_trailing_pct(code)
                    )
                    / 100.0
                )
            )
            trend_break = ind.ma20 is not None and ind.ma60 is not None and float(ind.ma20) < float(ind.ma60)
            core_ma200_break = False
            if code == core_symbol and bool(getattr(STRATEGY, "core_exit_on_ma200_break", True)):
                try:
                    core_ma200_break = ind.ma200 is not None and float(ind.close) < float(ind.ma200)
                except Exception:
                    core_ma200_break = False
            hold_days = _holding_days_on_kdf(str(h.buy_time), kdf)
            sell_locked = hold_days < int(STRATEGY.min_holding_days)
            if stop_loss:
                sl = float(getattr(STRATEGY, "core_stop_loss_pct", 15.0)) if code == core_symbol else float(_alpha_stop_loss_pct(code))
                reason = f"STOP_LOSS -{sl:.1f}% triggered (pnl={pnl_pct:.2f}%)"
                sell_qty = int(h.qty)
            elif code == core_symbol and core_ma200_break:
                reason = f"TREND_BREAK triggered (close<MA200)"
                sell_qty = int(h.qty)
            elif trail_hit:
                reason = (
                    f"TRAILING_STOP triggered (peak={h.highest_price_since_entry:.4f}, "
                    f"drawdown={_alpha_trailing_pct(code):.1f}%)"
                )
                sell_qty = int(h.qty)
            elif trend_break:
                reason = "TREND_BREAK triggered (MA20<MA60)"
                sell_qty = int(h.qty)
            elif ar.signal == "SELL":
                reason = f"TECH_SELL: {ar.reason}"
                sell_qty = int(h.qty)
            else:
                sell_qty = int(h.qty)
            if sell_locked and (not stop_loss) and reason:
                reason = f"HOLD_LOCK(<{STRATEGY.min_holding_days}d): {reason}"
                sell_qty = 0

            append_trade_log(
                {
                    "code": code,
                    "action": "HOLDING_CHECK",
                    "current_price": current,
                    "qty": h.qty,
                    "ma60": ind.ma60,
                    "market_mode": market_mode,
                    "account_drawdown_pct": risk_state.drawdown_pct,
                    "message": (
                        f"buy_price={h.buy_price}; pnl_pct={pnl_pct:.2f}; "
                        f"peak={h.highest_price_since_entry:.4f}; trailing_armed={h.trailing_armed}; "
                        f"partial_tp_done={h.partial_take_profit_done}; hold_days={hold_days}; {reason or 'no exit'}"
                    ),
                }
            )

            if not reason or sell_qty <= 0:
                continue

            broker_qty = int(pos_map.get(code, 0))
            held_qty = min(int(sell_qty), broker_qty) if broker_qty > 0 else int(sell_qty)
            plan = build_sell_plan(
                code=code,
                current_price=current,
                limit_price_factor=TRADE.limit_price_factor,
                max_order_usd=TRADE.max_order_usd,
                held_qty=held_qty,
            )
            if plan is None:
                continue
            # Add orderbook + sentiment/news context to SELL reason for manual decision.
            try:
                ctx = summarize_sell_context(
                    code=code,
                    current_price=float(current),
                    buy_price=float(h.buy_price),
                    pnl_pct=float(pnl_pct),
                    quote_ctx=broker.quote_ctx,
                )
                if ctx:
                    reason = reason + "\n" + ctx
            except Exception:
                pass
            _create_pending_and_notify(
                pending_orders=pending_orders,
                cache_items=cache_items,
                notifier=notifier,
                code=code,
                side="SELL",
                qty=plan.qty,
                limit_price=plan.limit_price,
                current_price=current,
                score=0,
                reason=reason,
                holding_count=len([x for x in holdings if int(x.qty) > 0 and str(x.symbol).upper() != core_symbol]),
                available_cash=available_cash,
                market_mode=market_mode,
                account_drawdown_pct=risk_state.drawdown_pct,
                candidate_rank="HOLDING_EXIT",
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[ERROR] holding-check {code} {err}")
            append_trade_log({"code": code, "action": "ERROR", "error": err})
    if holdings_changed:
        save_holdings(holdings)

    # 3) Buy opportunities with top-1 selection
    market_allows_buy = market_mode == "ATTACK"
    if not market_allows_buy:
        append_trade_log({"action": "BUY_SKIPPED", "market_mode": market_mode, "message": "DEFENSE mode; no new buys"})
    if buy_paused:
        append_trade_log(
            {
                "action": "BUY_SKIPPED",
                "market_mode": market_mode,
                "account_drawdown_pct": risk_state.drawdown_pct,
                "message": f"risk pause active until {risk_state.pause_buy_until}; reason={risk_state.pause_reason}",
            }
        )

    risk_overlay = RiskOverlayAdjustments(tone="NORMAL", rank_top_pct_bump=0.0, message="overlay_off")
    effective_rank_top = float(STRATEGY.rank_top_pct_threshold)
    if bool(getattr(STRATEGY, "risk_overlay_enabled", False)) and market_allows_buy:
        try:
            voo_for_overlay = core_kdf if isinstance(core_kdf, pd.DataFrame) and not core_kdf.empty else pd.DataFrame()
            risk_overlay = build_overlay(
                voo_df=voo_for_overlay,
                vix_cautious=float(STRATEGY.vix_cautious_level),
                vix_capital=float(STRATEGY.vix_capital_level),
                sentiment_cautious=float(STRATEGY.sentiment_cautious),
                sentiment_capital=float(STRATEGY.sentiment_capital),
                bump_cautious=float(STRATEGY.overlay_rank_bump_cautious),
                bump_capital=float(STRATEGY.overlay_rank_bump_capital),
            )
        except Exception as e:
            risk_overlay = RiskOverlayAdjustments(
                tone="NORMAL", rank_top_pct_bump=0.0, message=f"overlay_error:{type(e).__name__}:{e}"
            )
        effective_rank_top = min(
            0.95, float(STRATEGY.rank_top_pct_threshold) + float(risk_overlay.rank_top_pct_bump)
        )
        append_trade_log(
            {
                "action": "RISK_OVERLAY",
                "market_mode": market_mode,
                "message": (
                    f"{risk_overlay.message}; rank_base={STRATEGY.rank_top_pct_threshold:.3f} "
                    f"rank_eff={effective_rank_top:.3f}"
                ),
            }
        )
        if risk_overlay.tone == "CAPITAL_PRESERVATION":
            append_trade_log(
                {
                    "action": "ADVISORY_HEDGE",
                    "message": (
                        "避险风险叠加：可考虑手动降低敞口或对冲（如 inverse ETF）；"
                        "本程序不自动下反向单（与现有人工确认逻辑一致）。"
                    ),
                }
            )

    constraints = PositionConstraints(
        max_trade_cash_pct=0.0,
        max_symbol_asset_pct=float(STRATEGY.alpha_ratio) * float(STRATEGY.alpha_single_max_pct),
        max_total_asset_pct=float(STRATEGY.alpha_ratio),
        max_holding_count=int(STRATEGY.alpha_max_holding_count),
    )
    raw_candidates: list[dict] = []
    for code_u in [c.upper() for c in symbols]:
        try:
            kdf = fetch_daily_kline(broker.quote_ctx, code_u, days=700)
            if len(kdf) < STRATEGY.min_trading_days:
                raise RuntimeError(f"Not enough daily bars: got={len(kdf)} need>={STRATEGY.min_trading_days}")
            ind = latest_indicators(add_indicators(kdf))
            current = get_last_price(broker.quote_ctx, code_u)
            raw_candidates.append({"code": code_u, "ind": ind, "current": float(current), "kdf": kdf})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[ERROR] {code_u} {err}")
            append_trade_log({"code": code_u, "action": "ERROR", "error": err})

    ret_values = sorted([float(x["ind"].ret63_pct) for x in raw_candidates if x["ind"].ret63_pct is not None], reverse=True)
    rank_map: dict[str, float] = {}
    for item in raw_candidates:
        v = item["ind"].ret63_pct
        if v is None or not ret_values:
            rank_map[item["code"]] = 0.0
            continue
        idx = ret_values.index(float(v))
        rank_map[item["code"]] = 1.0 if len(ret_values) == 1 else 1.0 - (idx / float(len(ret_values) - 1))

    scored: list[dict] = []
    for item in raw_candidates:
        code_u = item["code"]
        ind = item["ind"]
        current = float(item["current"])
        kdf = item["kdf"]
        rank_pct = float(rank_map.get(code_u, 0.0))
        rank_top = rank_pct >= float(effective_rank_top)
        strong_exception = code_u in set(TRADE.strong_exception_symbols)
        ignore_rsi = code_u in set(TRADE.no_rsi_limit_symbols)
        ar = analyze(
            code_u,
            ind,
            buy_threshold=STRATEGY.buy_score_threshold,
            market_ok=market_allows_buy,
            rank_top=rank_top,
            market_mode=market_mode,
            strong_exception=strong_exception,
            rsi_upper=STRATEGY.strong_rsi_upper,
            ignore_rsi=ignore_rsi,
        )
        relaxed_entry = (
            ind.ma100 is not None
            and ind.ma20 is not None
            and ind.ma60 is not None
            and float(current) > float(ind.ma100)
            and float(ind.ma20) > float(ind.ma60)
            and rank_pct >= float(STRATEGY.buy_relaxed_min_rank_pct)
            and int(ar.score) >= int(STRATEGY.buy_relaxed_min_base_score)
            and (ind.ret5d_pct is None or float(ind.ret5d_pct) <= float(STRATEGY.buy_max_ret5d_pct))
            and (ind.rsi14 is None or float(ind.rsi14) <= float(STRATEGY.buy_max_rsi))
        )
        if market_allows_buy and ar.signal != "BUY" and relaxed_entry:
            ar = analyze(
                code_u,
                ind,
                buy_threshold=0,
                market_ok=True,
                rank_top=True,
                market_mode=market_mode,
                strong_exception=strong_exception,
                rsi_upper=STRATEGY.strong_rsi_upper,
                ignore_rsi=True,
            )
            ar = type(ar)(
                code=ar.code,
                score=max(int(ar.score), int(STRATEGY.buy_score_threshold)),
                signal="BUY",
                indicators=ar.indicators,
                reason=(
                    f"RELAXED_ENTRY: price>MA100 & MA20>MA60 "
                    f"& rank>={float(STRATEGY.buy_relaxed_min_rank_pct) * 100.0:.0f}% "
                    f"& base_score>={int(STRATEGY.buy_relaxed_min_base_score)}; {ar.reason}"
                ),
                market_mode=ar.market_mode,
            )
        scored.append(
            {
                "code": code_u,
                "ar": ar,
                "current": current,
                "rank_pct": rank_pct,
                "kdf": kdf,
                "relaxed_entry": relaxed_entry and ar.signal == "BUY",
            }
        )
        append_trade_log(
            {
                "code": code_u,
                "score": ar.score,
                "action": ar.signal,
                "current_price": current,
                "ma20": ar.indicators.ma20,
                "ma60": ar.indicators.ma60,
                "ma100": ar.indicators.ma100,
                "ma200": ar.indicators.ma200,
                "rsi14": ar.indicators.rsi14,
                "ret5d_pct": ar.indicators.ret5d_pct,
                "ret63_pct": ar.indicators.ret63_pct,
                "market_mode": market_mode,
                "account_drawdown_pct": risk_state.drawdown_pct,
                "candidate_rank": f"{rank_pct * 100.0:.1f}%",
                "message": f"{ar.reason}; strong_exception={strong_exception}; ignore_rsi={ignore_rsi}",
            }
        )
        print(f"{code_u} signal={ar.signal} score={ar.score} rank={rank_pct * 100.0:.1f}% | {ar.reason}")

    ranking_text = _format_candidate_ranking([{"code": x["code"], "score": x["ar"].score} for x in scored])
    price_map = {str(x["code"]).upper(): float(x["current"]) for x in scored}
    core_price = float(price_map.get(core_symbol, get_last_price(broker.quote_ctx, core_symbol)))
    core_value = float(core_qty) * float(core_price)
    alpha_value = max(0.0, float(total_assets) - float(core_value))
    alloc_text = f"CORE={core_value:.2f}({(core_value/total_assets*100.0) if total_assets>0 else 0.0:.1f}%), ALPHA={alpha_value:.2f}"
    p_state = _load_json(
        PORTFOLIO_STATE_PATH,
        {
            "base_total_assets": float(total_assets) if total_assets > 0 else 1.0,
            "base_core_value": float(core_value),
            "base_alpha_value": float(alpha_value),
        },
    )
    if not p_state.get("initialized", False):
        p_state["base_total_assets"] = float(total_assets) if total_assets > 0 else 1.0
        p_state["base_core_value"] = float(core_value) if core_value > 0 else 1.0
        p_state["base_alpha_value"] = float(alpha_value) if alpha_value > 0 else 1.0
        p_state["initialized"] = True
    _save_json(PORTFOLIO_STATE_PATH, p_state)
    base_total = float(p_state.get("base_total_assets", 1.0) or 1.0)
    base_core = float(p_state.get("base_core_value", 1.0) or 1.0)
    base_alpha = float(p_state.get("base_alpha_value", 1.0) or 1.0)
    portfolio_ret = ((float(total_assets) / base_total) - 1.0) * 100.0 if base_total > 0 else 0.0
    core_ret = ((float(core_value) / base_core) - 1.0) * 100.0 if base_core > 0 else 0.0
    alpha_ret = ((float(alpha_value) / base_alpha) - 1.0) * 100.0 if base_alpha > 0 else 0.0
    _save_runtime_snapshot(
        market_mode=market_mode,
        account_drawdown_pct=risk_state.drawdown_pct,
        candidate_ranking=ranking_text,
        portfolio_return_pct=portfolio_ret,
        core_return_pct=core_ret,
        alpha_return_pct=alpha_ret,
        outperform_voo=portfolio_ret > core_ret,
        allocation_text=alloc_text,
    )

    if total_assets > 0:
        enh_set = {str(s).upper() for s in TRADE.high_vol_alpha_symbols}
        enh_val = 0.0
        for h in holdings:
            if int(h.qty) <= 0:
                continue
            sym = str(h.symbol).upper()
            if sym not in enh_set:
                continue
            px = float(price_map.get(sym, float(h.buy_price) or 0.0))
            enh_val += float(int(h.qty) * px)
        cap_amt = float(STRATEGY.alpha_enhancement_max_pct) / 100.0 * float(total_assets)
        if enh_val > cap_amt:
            append_trade_log(
                {
                    "action": "ADVISORY_ENHANCEMENT_CAP",
                    "message": (
                        f"high_vol_notional={enh_val:.2f} USD vs soft_cap={cap_amt:.2f} "
                        f"({float(STRATEGY.alpha_enhancement_max_pct):.1f}% of assets); not enforced (existing logic)"
                    ),
                }
            )

    filtered_buy_pool: list[dict] = []
    for x in scored:
        if x["ar"].signal != "BUY":
            continue
        code_u = str(x["code"])
        if str(code_u).upper() == core_symbol:
            continue
        kdf = x["kdf"]
        bucket = _alpha_bucket(code_u)
        quality_blocks = _buy_entry_quality_blocks(
            code=code_u,
            ind=x["ar"].indicators,
            bucket=bucket,
            rank_pct=float(x["rank_pct"]),
            relaxed_entry=bool(x.get("relaxed_entry", False)),
        )
        if quality_blocks:
            append_trade_log(
                {
                    "code": code_u,
                    "action": "BUY_BLOCKED",
                    "market_mode": market_mode,
                    "message": "entry quality guard: " + "; ".join(quality_blocks),
                }
            )
            continue
        holding_any = get_holding_any(holdings, code_u)
        if holding_any is not None and int(getattr(holding_any, "qty", 0)) > 0:
            if not _alpha_allow_add(code_u):
                append_trade_log({"code": code_u, "action": "BUY_BLOCKED", "message": "add disabled for TREND_CORE"})
                continue
            add_count = int(getattr(holding_any, "add_position_count", 0))
            if add_count >= int(STRATEGY.v8_max_add_count):
                append_trade_log({"code": code_u, "action": "BUY_BLOCKED", "message": "max add_position_count reached"})
                continue
            if not _is_breakout_high(kdf, lookback_days=STRATEGY.v8_breakout_lookback_days):
                append_trade_log({"code": code_u, "action": "BUY_BLOCKED", "message": "add blocked: no breakout above recent high"})
                continue
            x2 = dict(x)
            x2["is_add"] = True
            x2["holding_any"] = holding_any
            filtered_buy_pool.append(x2)
            continue

        if int(pos_map.get(code_u, 0)) > 0:
            append_trade_log({"code": code_u, "action": "BUY_BLOCKED", "message": "broker position exists"})
            continue

        # Entry hardening for HIGH_VOL: higher threshold + breakout required + longer post-sell cooldown.
        if bucket == "HIGH_VOL":
            if int(x["ar"].score) < int(STRATEGY.alpha_high_vol_buy_score_threshold):
                append_trade_log(
                    {
                        "code": code_u,
                        "action": "BUY_BLOCKED",
                        "market_mode": market_mode,
                        "message": f"HIGH_VOL score<{STRATEGY.alpha_high_vol_buy_score_threshold}",
                    }
                )
                continue
            if bool(STRATEGY.alpha_high_vol_require_breakout) and not _is_breakout_high(
                kdf, lookback_days=int(STRATEGY.v8_breakout_lookback_days)
            ):
                append_trade_log(
                    {
                        "code": code_u,
                        "action": "BUY_BLOCKED",
                        "market_mode": market_mode,
                        "message": f"HIGH_VOL entry requires breakout{int(STRATEGY.v8_breakout_lookback_days)}d high",
                    }
                )
                continue

        min_reentry_days = (
            int(STRATEGY.alpha_trend_reentry_wait_days)
            if bucket == "TREND_CORE"
            else int(STRATEGY.alpha_high_vol_reentry_wait_days)
            if bucket == "HIGH_VOL"
            else int(STRATEGY.reentry_min_days_since_sell)
        )
        if not _allow_reentry(code_u, kdf, holding_any, min_days_since_sell=min_reentry_days):
            append_trade_log(
                {
                    "code": code_u,
                    "action": "BUY_BLOCKED",
                    "market_mode": market_mode,
                    "message": f"reentry condition not met (breakout20 or min-days-since-sell={min_reentry_days})",
                }
            )
            continue
        x2 = dict(x)
        x2["is_add"] = False
        x2["holding_any"] = holding_any
        filtered_buy_pool.append(x2)
    if market_allows_buy and (not buy_paused) and filtered_buy_pool:
        existing_exposure = 0.0
        for h in holdings:
            if int(h.qty) <= 0:
                continue
            px = float(price_map.get(str(h.symbol).upper(), float(h.buy_price)))
            existing_exposure += float(int(h.qty) * px)
        planned_exposure = float(existing_exposure)
        planned_new_nonadd = 0
        ordered = sorted(filtered_buy_pool, key=lambda x: (-int(x["ar"].score), -float(x["rank_pct"]), str(x["code"])))[:3]
        for best in ordered:
            code_u = best["code"]
            current = float(best["current"])
            ar = best["ar"]
            base_plan = build_buy_plan(
                code=code_u,
                current_price=current,
                limit_price_factor=TRADE.limit_price_factor,
                max_order_usd=TRADE.max_order_usd,
                min_order_qty=TRADE.min_order_qty,
            )
            if base_plan is None:
                continue
            current_symbol_value = int(pos_map.get(code_u, 0)) * current
            active_holdings_count = len(
                [x for x in holdings if int(x.qty) > 0 and str(x.symbol).upper() != core_symbol]
            )
            is_add = bool(best.get("is_add", False))
            holding_any = best.get("holding_any")
            sleeve_pct = _alpha_buy_cash_pct(code_u)
            cash_pct = float(STRATEGY.alpha_ratio) * float(sleeve_pct)
            custom_constraints = PositionConstraints(
                max_trade_cash_pct=cash_pct,
                max_symbol_asset_pct=constraints.max_symbol_asset_pct,
                max_total_asset_pct=constraints.max_total_asset_pct,
                max_holding_count=constraints.max_holding_count,
            )
            size = calc_buy_qty(
                available_cash=available_cash,
                total_assets=total_assets if total_assets > 0 else available_cash,
                current_symbol_value=current_symbol_value,
                current_total_exposure_value=planned_exposure,
                current_holding_count=(0 if is_add else active_holdings_count + planned_new_nonadd),
                has_existing_position=False,
                limit_price=base_plan.limit_price,
                max_order_value_usd=TRADE.max_order_usd,
                constraints=custom_constraints,
                volatility_multiplier=1.0,
            )
            final_qty = int(size.qty)
            if is_add and holding_any is not None:
                max_add_qty = int(getattr(holding_any, "initial_qty", 0) or 0)
                if max_add_qty > 0:
                    final_qty = min(final_qty, int(max_add_qty))
            if size.allowed and final_qty >= 1:
                planned_exposure += float(final_qty) * float(base_plan.limit_price)
                if not is_add:
                    planned_new_nonadd += 1
                _create_pending_and_notify(
                    pending_orders=pending_orders,
                    cache_items=cache_items,
                    notifier=notifier,
                    code=code_u,
                    side="BUY",
                    qty=final_qty,
                    limit_price=base_plan.limit_price,
                    current_price=current,
                    score=ar.score,
                    reason=(
                        f"{ar.reason}; rank={best['rank_pct']*100.0:.1f}%; cash_pct={cash_pct:.2f}; "
                        f"{'ADD_POSITION' if is_add else 'INITIAL_ENTRY'}"
                    ),
                    holding_count=active_holdings_count,
                    available_cash=available_cash,
                    market_mode=market_mode,
                    account_drawdown_pct=risk_state.drawdown_pct,
                    candidate_rank=f"{best['rank_pct']*100.0:.1f}%",
                )
            else:
                msg = size.reason if not size.allowed else "add qty capped below 1 by initial position size"
                append_trade_log({"code": code_u, "action": "BUY_BLOCKED", "message": msg})
    elif market_allows_buy and (not buy_paused):
        append_trade_log({"action": "BUY_SKIPPED", "market_mode": market_mode, "message": "no BUY candidate >= threshold"})

    # 4) Remote manual confirmation and execution
    process_wechat_confirmations(broker=broker, notifier=notifier)
    process_pushplus_confirmations(broker=broker, notifier=notifier)
    _confirm_and_execute_pending(broker, notifier)


def _maybe_send_daily_report_task(broker: FutuLiveBroker, notifier: PushPlusNotifier) -> None:
    try:
        target = STRATEGY.daily_report_local_hhmm
        if not should_send_daily_push_now(target):
            return
        summary = build_daily_summary(broker=broker)
        write_daily_report_csv(summary)
        ok, msg = notifier.send(title="TradePilot 每日交易日志", content=format_daily_push_content(summary))
        if ok:
            mark_daily_push_sent()
            append_trade_log({"action": "DAILY_REPORT_PUSH", "message": f"time={target} ok={ok} resp={msg}"})
        else:
            # Do not mark sent on failure; allow retry in next cycles.
            append_trade_log({"action": "REPORT_ERROR", "error": f"daily_push_failed: time={target} resp={msg}"})
    except Exception as e:
        append_trade_log({"action": "REPORT_ERROR", "error": f"{type(e).__name__}: {e}"})


def _maybe_evaluate_verdict_outcomes() -> None:
    """每天评估已推送7天以上、尚未评估的舆情信号 outcome，并更新关键词权重。"""
    try:
        n = evaluate_pending_outcomes()
        if n > 0:
            logging.info("[VERDICT] 评估完成 updated=%d", n)
    except Exception as e:
        logging.warning("[VERDICT] outcome评估异常: %s", e)


def _maybe_send_weekly_verdict_review(notifier: PushPlusNotifier) -> None:
    """每周一 UTC 09:00 发送舆情利好/利空信号追踪周报。"""
    try:
        weekly_weekday = int(os.getenv("TP_WEEKLY_REVIEW_WEEKDAY", "0"))   # 0=周一
        weekly_hhmm = os.getenv("TP_WEEKLY_REVIEW_HHMM", "09:00")
        if not should_send_weekly_review_now(target_weekday=weekly_weekday, target_hhmm=weekly_hhmm):
            return
        content = build_weekly_verdict_push()
        # 从内容里提取准确率用于下周对比
        acc_pct = None
        for line in content.splitlines():
            if "准确率:" in line:
                try:
                    acc_pct = float(line.split("准确率:")[-1].replace("%", "").strip())
                except Exception:
                    pass
                break
        ok, msg = notifier.send(title="📊 TradePilot 舆情周报", content=content)
        if ok:
            mark_weekly_review_sent(accuracy_pct=acc_pct)
            logging.info("[WEEKLY] 舆情周报已发送 accuracy_pct=%s", acc_pct)
        else:
            logging.warning("[WEEKLY] 舆情周报发送失败: %s", msg)
    except Exception as e:
        logging.warning("[WEEKLY] 舆情周报异常: %s", e)


def main() -> None:
    # Prefer writing logs next to the executable (packaged) or cwd (source run).
    # Keep it simple and robust across environments.
    log_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "logs")
    os.makedirs(log_dir, exist_ok=True)
    setup_logging(log_dir=log_dir)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    broker = FutuLiveBroker(
        FutuLiveBrokerConfig(
            host=FUTU.host,
            port=FUTU.port,
            max_single_notional_usd=TRADE.max_order_usd,
        )
    )
    notifier = PushPlusNotifier(token_env="PUSHPLUS_TOKEN")
    print(f"PushPlus enabled={notifier.enabled()} (env=PUSHPLUS_TOKEN)")
    print(f"Polling interval: {STRATEGY.poll_interval_seconds} seconds")
    try:
        import data_provider as _dp

        print(f"data_provider={getattr(_dp, '__file__', '')} version={getattr(_dp, 'DATA_PROVIDER_VERSION', '')}")
    except Exception:
        pass

    LAST_START_PATH = os.path.join(log_dir, "last_start.json")

    def _load_last_start() -> dict:
        try:
            if os.path.exists(LAST_START_PATH):
                with open(LAST_START_PATH, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception:
            return {}
        return {}

    def _save_last_start(ts_utc: str) -> None:
        try:
            os.makedirs(os.path.dirname(LAST_START_PATH), exist_ok=True)
            with open(LAST_START_PATH, "w", encoding="utf-8") as f:
                json.dump({"ts_utc": ts_utc}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    _shutdown_pushed = {"done": False}

    def _safe_send_lifecycle(title: str, content: str, *, http_timeout: float = 20.0) -> None:
        try:
            ok, msg = notifier.send(title=title, content=content, timeout=http_timeout)
            append_trade_log({"action": "LIFECYCLE_PUSH", "title": title, "ok": ok, "message": msg})
        except Exception as e:
            append_trade_log({"action": "LIFECYCLE_PUSH_ERROR", "title": title, "error": f"{type(e).__name__}: {e}"})

    def _push_shutdown_once(reason: str, *, http_timeout: float = 14.0) -> None:
        if _shutdown_pushed["done"]:
            return
        _shutdown_pushed["done"] = True
        # Disk first: reboot often kills the process before HTTP returns.
        write_shutdown_hook(reason)
        try:
            now_local = _utc_iso_to_local_str(_now_iso())
            _safe_send_lifecycle(
                "TradePilot 关闭",
                f"time_local={now_local}\nreason={reason}",
                http_timeout=http_timeout,
            )
        except Exception:
            pass

    # Ensure we push on normal interpreter exit (best-effort).
    atexit.register(lambda: _push_shutdown_once("atexit", http_timeout=14.0))

    # Best-effort handling for console close/logoff/shutdown on Windows.
    # This helps when user clicks the console window [X].
    try:
        if os.name == "nt":
            CTRL_C_EVENT = 0
            CTRL_BREAK_EVENT = 1
            CTRL_CLOSE_EVENT = 2
            CTRL_LOGOFF_EVENT = 5
            CTRL_SHUTDOWN_EVENT = 6

            HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def _console_ctrl_handler(ctrl_type: int) -> bool:
                if ctrl_type in (
                    CTRL_C_EVENT,
                    CTRL_BREAK_EVENT,
                    CTRL_CLOSE_EVENT,
                    CTRL_LOGOFF_EVENT,
                    CTRL_SHUTDOWN_EVENT,
                ):
                    # Short HTTP timeout: OS only grants a few seconds on shutdown.
                    _push_shutdown_once(f"console_ctrl_{int(ctrl_type)}", http_timeout=5.0)
                    return False  # allow default handling to proceed
                return False

            ctypes.windll.kernel32.SetConsoleCtrlHandler(HandlerRoutine(_console_ctrl_handler), True)
    except Exception:
        pass

    try:
        start_windows_session_listener(
            on_session_end=lambda r: _push_shutdown_once(r, http_timeout=5.0),
        )
    except Exception:
        pass

    try:
        broker.connect()
        # Start the independent news monitor thread.
        try:
            def _news_symbols():
                syms = _alpha_cycle_symbols()
                core_sym = str(TRADE.core_symbol).strip().upper()
                return sorted(set(syms + ([core_sym] if core_sym else [])))
            _start_news_monitor_thread(notifier, _news_symbols)
        except Exception as _exc:
            print(f"[NEWS] 启动监控线程失败: {_exc}", flush=True)
        # Start ClawBot long-poll listener thread (real-time WeChat message handling).
        try:
            start_clawbot_listener(broker=broker, notifier=notifier)
        except Exception as _exc:
            print(f"[ClawBot] 启动监听线程失败: {_exc}", flush=True)
        try:
            start_clawbot_bridge_process()
        except Exception as _exc:
            print(f"[ClawBot] 启动 Cursor AI 桥接失败: {_exc}", flush=True)
        # Startup push
        try:
            now_iso = _now_iso()
            now_local = _utc_iso_to_local_str(now_iso)
            last = _load_last_start()
            last_ts = str(last.get("ts_utc", "") or "").strip()
            title = "TradePilot 启动"
            if last_ts:
                title = "TradePilot 重启"
            _safe_send_lifecycle(
                title,
                f"time_local={now_local}\nmode=pushplus_confirm_enabled\npoll_interval={STRATEGY.poll_interval_seconds}s",
                http_timeout=12.0,
            )
            _save_last_start(now_iso)
        except Exception:
            pass
        while True:
            print("\n========== New Poll Cycle ==========")
            try:
                _run_once(broker, notifier)
                _maybe_send_daily_report_task(broker, notifier)
                _maybe_evaluate_verdict_outcomes()
                _maybe_send_weekly_verdict_review(notifier)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print(f"[CYCLE ERROR] {err}")
                print(traceback.format_exc())
                append_trade_log({"action": "CYCLE_ERROR", "error": err})

            print(f"Sleeping {STRATEGY.poll_interval_seconds} seconds...\n")
            time.sleep(STRATEGY.poll_interval_seconds)
    except KeyboardInterrupt:
        print("Stopped by user.")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[FATAL] {err}")
        print(traceback.format_exc())
        append_trade_log({"action": "FATAL", "error": err})
    finally:
        _push_shutdown_once("finally", http_timeout=14.0)
        try:
            broker.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # If packaged EXE flashes and closes, persist the traceback.
        try:
            base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            crash_dir = os.path.join(base_dir, "logs")
            os.makedirs(crash_dir, exist_ok=True)
            with open(os.path.join(crash_dir, "fatal_startup.log"), "a", encoding="utf-8") as f:
                f.write("\n==== fatal_startup ====\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise

