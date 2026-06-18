from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
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


def _format_local_holdings() -> str:
    holdings = [h for h in load_holdings() if int(h.qty) > 0]
    if not holdings:
        return "\u65e0\u6301\u4ed3"
    return ", ".join([f"{h.symbol}:{h.qty}@{h.buy_price:.2f}" for h in holdings])


def _row_first(row, names: tuple[str, ...], default=0):
    for name in names:
        value = row.get(name, None)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _format_broker_holdings(broker) -> Optional[str]:
    if broker is None:
        return None
    try:
        df = broker.get_positions()
    except Exception:
        return None
    if df is None or df.empty:
        return "\u65e0\u6301\u4ed3"

    lines: list[str] = []
    for _, row in df.iterrows():
        try:
            qty = int(float(_row_first(row, ("qty", "position_qty", "holding_qty", "can_sell_qty"), 0) or 0))
        except Exception:
            qty = 0
        if qty <= 0:
            continue

        symbol = str(_row_first(row, ("code", "stock_code", "security_code", "symbol"), "") or "").strip().upper()
        if not symbol:
            continue
        try:
            cost = float(_row_first(row, ("cost_price", "pl_cost_price", "average_cost", "avg_cost"), 0) or 0)
        except Exception:
            cost = 0.0
        try:
            market_val = float(_row_first(row, ("market_val", "market_value", "position_market_value"), 0) or 0)
        except Exception:
            market_val = 0.0

        if market_val > 0:
            lines.append(f"{symbol}:{qty}@{cost:.2f} value={market_val:.2f}")
        else:
            lines.append(f"{symbol}:{qty}@{cost:.2f}")

    return ", ".join(lines) if lines else "\u65e0\u6301\u4ed3"


def _looks_like_no_holdings(text: str) -> bool:
    return _clean_holding_text(text) == "\u65e0\u6301\u4ed3"


def _has_non_cash_assets(*, cash: float, assets: float) -> bool:
    if assets <= 0:
        return False
    non_cash = float(assets) - max(float(cash), 0.0)
    return non_cash > max(1.0, float(assets) * 0.01)


def _format_unknown_broker_holdings(*, cash: float, assets: float) -> str:
    non_cash = max(0.0, float(assets) - max(float(cash), 0.0))
    return f"\u6301\u4ed3\u6570\u636e\u672a\u8fd4\u56de\uff08\u975e\u73b0\u91d1\u8d44\u4ea7\u7ea6 {non_cash:.2f} USD\uff09"


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

    realized = _calc_realized_pnl_from_rows(rows)
    try:
        cash = float(broker.get_available_cash())
    except Exception:
        cash = 0.0
    try:
        assets = float(broker.get_total_assets())
    except Exception:
        assets = 0.0

    holding_text = _format_broker_holdings(broker)
    if holding_text is None:
        holding_text = _format_local_holdings()
    if _looks_like_no_holdings(holding_text) and _has_non_cash_assets(cash=cash, assets=assets):
        holding_text = _format_unknown_broker_holdings(cash=cash, assets=assets)

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


def _clean_daily_text(value: object) -> str:
    text = str(value or "").strip()
    return {
        "鏃犳寔浠?": "\u65e0\u6301\u4ed3",
        "鏃犲紓甯?": "\u65e0\u5f02\u5e38",
        "鏃?": "\u65e0",
    }.get(text, text)


def _clean_holding_text(value: object) -> str:
    text = _clean_daily_text(value)
    return "\u65e0\u6301\u4ed3" if "?" in text else text


def _clean_error_text(value: object) -> str:
    text = _clean_daily_text(value)
    return "\u65e0\u5f02\u5e38" if "?" in text else text


def format_daily_push_content(summary: DailySummary) -> str:
    return (
        f"\u62a5\u544a\u65e5\u671f(\u672c\u5730): {summary.date_local}\n"
        f"\u4eca\u65e5\u4fe1\u53f7\u6570\u91cf: {summary.signal_count}\n"
        f"\u4eca\u65e5\u4e0b\u5355\u6b21\u6570: {summary.order_count}\n"
        f"\u4eca\u65e5\u6210\u4ea4\u6b21\u6570: {summary.filled_count}\n"
        f"\u5f53\u524d\u6301\u4ed3: {_clean_holding_text(summary.holding_text)}\n"
        f"\u4eca\u65e5\u5df2\u5b9e\u73b0\u76c8\u4e8f: {summary.realized_pnl:.2f} USD\n"
        f"\u5f53\u524d\u8d26\u6237\u73b0\u91d1: {summary.available_cash:.2f} USD\n"
        f"\u5f53\u524d\u8d26\u6237\u603b\u8d44\u4ea7: {summary.total_assets:.2f} USD\n"
        f"\u5f53\u524d\u5e02\u573a\u6a21\u5f0f: {_clean_daily_text(summary.market_mode)}\n"
        f"\u8d26\u6237\u56de\u64a4: {summary.account_drawdown_pct:.2f}%\n"
        f"\u4eca\u65e5\u5019\u9009\u6392\u540d: {_clean_daily_text(summary.candidate_ranking)}\n"
        f"\u5f53\u524d\u7ec4\u5408\u6536\u76ca: {summary.portfolio_return_pct:.2f}%\n"
        f"CORE\u6536\u76ca: {summary.core_return_pct:.2f}%\n"
        f"ALPHA\u6536\u76ca: {summary.alpha_return_pct:.2f}%\n"
        f"\u662f\u5426\u8dd1\u8d62VOO: {'YES' if summary.outperform_voo else 'NO'}\n"
        f"\u5f53\u524d\u4ed3\u4f4d\u5206\u5e03: {_clean_daily_text(summary.allocation_text)}\n"
        f"\u4eca\u65e5\u5f02\u5e38\u6458\u8981: {_clean_error_text(summary.error_summary)}"
    )
