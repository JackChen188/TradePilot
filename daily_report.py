from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import STRATEGY
from holdings import load_holdings


TRADE_LOG_PATH = os.path.join("logs", "trade_log.csv")
DAILY_REPORT_PATH = os.path.join("logs", "daily_report.csv")
DAILY_PUSH_STATE_PATH = os.path.join("logs", "daily_report_push_state.json")
RUNTIME_SNAPSHOT_PATH = os.path.join("logs", "runtime_snapshot.json")


@dataclass(frozen=True)
class DailySummary:
    date_local: str
    signal_count: int
    order_count: int
    filled_count: int
    holding_text: str
    realized_pnl: float
    available_cash: float
    total_assets: float
    error_summary: str
    market_mode: str
    account_drawdown_pct: float
    candidate_ranking: str
    portfolio_return_pct: float
    core_return_pct: float
    alpha_return_pct: float
    outperform_voo: bool
    allocation_text: str


def _today_local() -> str:
    tz_name = (getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip()
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _now_local_hhmm() -> str:
    tz_name = (getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip()
    return datetime.now(ZoneInfo(tz_name)).strftime("%H:%M")


def _load_state(path: str = DAILY_PUSH_STATE_PATH) -> dict:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or {}


def _save_state(state: dict, path: str = DAILY_PUSH_STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_send_daily_push_now(target_local_hhmm: str) -> bool:
    state = _load_state()
    today = _today_local()
    if state.get("last_sent_date") == today:
        return False
    return _now_local_hhmm() >= target_local_hhmm


def mark_daily_push_sent() -> None:
    state = _load_state()
    state["last_sent_date"] = _today_local()
    _save_state(state)


def _load_today_rows(path: str = TRADE_LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    today = _today_local()
    out: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = str(row.get("ts_utc", ""))
            try:
                dt_utc = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                tz_name = (getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip()
                if dt_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d") == today:
                    out.append(row)
            except Exception:
                continue
    return out


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _calc_realized_pnl_from_rows(rows: list[dict]) -> float:
    # Simple FIFO-like same-day realized pnl estimation from executed BUY/SELL rows.
    inv: dict[str, dict[str, float]] = {}
    pnl = 0.0
    for r in rows:
        action = str(r.get("action", "")).upper()
        if action not in ("BUY", "SELL"):
            continue
        if _to_int(r.get("selected", 0)) != 1:
            continue
        if _to_int(r.get("order_ok", 0)) != 1:
            continue
        symbol = str(r.get("code", "")).upper()
        qty = _to_int(r.get("qty", 0))
        price = _to_float(r.get("limit_price", 0))
        if not symbol or qty <= 0 or price <= 0:
            continue
        s = inv.setdefault(symbol, {"qty": 0, "cost": 0.0})
        if action == "BUY":
            s["cost"] += price * qty
            s["qty"] += qty
        else:
            if s["qty"] <= 0:
                continue
            m = min(qty, int(s["qty"]))
            avg = s["cost"] / s["qty"] if s["qty"] > 0 else 0.0
            pnl += (price - avg) * m
            s["qty"] -= m
            s["cost"] -= avg * m
    return float(pnl)


def _load_runtime_snapshot(path: str = RUNTIME_SNAPSHOT_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def build_daily_summary(*, broker, trade_log_path: str = TRADE_LOG_PATH) -> DailySummary:
    rows = _load_today_rows(trade_log_path)
    signal_count = sum(1 for r in rows if str(r.get("action", "")).upper() in ("BUY", "SELL") and _to_int(r.get("selected", 0)) == 0)
    order_count = sum(1 for r in rows if str(r.get("action", "")).upper() in ("BUY", "SELL") and _to_int(r.get("selected", 0)) == 1)
    filled_count = sum(
        1
        for r in rows
        if str(r.get("action", "")).upper() in ("BUY", "SELL")
        and _to_int(r.get("selected", 0)) == 1
        and any(x in str(r.get("order_status", "")).upper() for x in ("FILLED", "DEALT", "EXECUTED"))
    )
    errs = []
    for r in rows:
        a = str(r.get("action", "")).upper()
        e = str(r.get("error", "")).strip()
        if e:
            errs.append(e)
        elif any(x in a for x in ("ERROR", "FATAL", "CYCLE")):
            msg = str(r.get("message", "")).strip()
            if msg:
                errs.append(f"{a}: {msg}")
    err_summary = " | ".join(errs[:5]) if errs else "无异常"

    holdings = [h for h in load_holdings() if int(h.qty) > 0]
    holding_text = "无持仓"
    if holdings:
        holding_text = ", ".join([f"{h.symbol}:{h.qty}@{h.buy_price:.2f}" for h in holdings])

    realized = _calc_realized_pnl_from_rows(rows)
    try:
        cash = float(broker.get_available_cash())
    except Exception:
        cash = 0.0
    try:
        assets = float(broker.get_total_assets())
    except Exception:
        assets = 0.0
    snapshot = _load_runtime_snapshot()
    market_mode = str(snapshot.get("market_mode", "未知"))
    drawdown_pct = _to_float(snapshot.get("account_drawdown_pct", 0.0))
    ranking = str(snapshot.get("candidate_ranking", "无"))
    portfolio_ret = _to_float(snapshot.get("portfolio_return_pct", 0.0))
    core_ret = _to_float(snapshot.get("core_return_pct", 0.0))
    alpha_ret = _to_float(snapshot.get("alpha_return_pct", 0.0))
    outperform = bool(snapshot.get("outperform_voo", False))
    allocation_text = str(snapshot.get("allocation_text", "无"))

    return DailySummary(
        date_local=_today_local(),
        signal_count=signal_count,
        order_count=order_count,
        filled_count=filled_count,
        holding_text=holding_text,
        realized_pnl=realized,
        available_cash=cash,
        total_assets=assets,
        error_summary=err_summary,
        market_mode=market_mode,
        account_drawdown_pct=drawdown_pct,
        candidate_ranking=ranking,
        portfolio_return_pct=portfolio_ret,
        core_return_pct=core_ret,
        alpha_return_pct=alpha_ret,
        outperform_voo=outperform,
        allocation_text=allocation_text,
    )


def write_daily_report_csv(summary: DailySummary, path: str = DAILY_REPORT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = [
        "date_local",
        "signal_count",
        "order_count",
        "filled_count",
        "holding_text",
        "realized_pnl",
        "available_cash",
        "total_assets",
        "error_summary",
        "market_mode",
        "account_drawdown_pct",
        "candidate_ranking",
        "portfolio_return_pct",
        "core_return_pct",
        "alpha_return_pct",
        "outperform_voo",
        "allocation_text",
    ]
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(
            {
                "date_local": summary.date_local,
                "signal_count": summary.signal_count,
                "order_count": summary.order_count,
                "filled_count": summary.filled_count,
                "holding_text": summary.holding_text,
                "realized_pnl": f"{summary.realized_pnl:.2f}",
                "available_cash": f"{summary.available_cash:.2f}",
                "total_assets": f"{summary.total_assets:.2f}",
                "error_summary": summary.error_summary,
                "market_mode": summary.market_mode,
                "account_drawdown_pct": f"{summary.account_drawdown_pct:.2f}",
                "candidate_ranking": summary.candidate_ranking,
                "portfolio_return_pct": f"{summary.portfolio_return_pct:.2f}",
                "core_return_pct": f"{summary.core_return_pct:.2f}",
                "alpha_return_pct": f"{summary.alpha_return_pct:.2f}",
                "outperform_voo": "YES" if summary.outperform_voo else "NO",
                "allocation_text": summary.allocation_text,
            }
        )


def format_daily_push_content(summary: DailySummary) -> str:
    return (
        f"今日日期(本地): {summary.date_local}\n"
        f"今日信号数量: {summary.signal_count}\n"
        f"今日下单次数: {summary.order_count}\n"
        f"今日成交次数: {summary.filled_count}\n"
        f"当前持仓: {summary.holding_text}\n"
        f"今日已实现盈亏: {summary.realized_pnl:.2f}\n"
        f"币种: USD\n"
        f"当前账户现金(USD): {summary.available_cash:.2f}\n"
        f"当前账户总资产(USD): {summary.total_assets:.2f}\n"
        f"当前市场模式: {summary.market_mode}\n"
        f"账户回撤: {summary.account_drawdown_pct:.2f}%\n"
        f"今日候选排名: {summary.candidate_ranking}\n"
        f"当前组合收益: {summary.portfolio_return_pct:.2f}%\n"
        f"CORE收益: {summary.core_return_pct:.2f}%\n"
        f"ALPHA收益: {summary.alpha_return_pct:.2f}%\n"
        f"是否跑赢VOO: {'YES' if summary.outperform_voo else 'NO'}\n"
        f"当前仓位分布: {summary.allocation_text}\n"
        f"今日异常摘要: {summary.error_summary}"
    )

