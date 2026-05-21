from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from holdings import Holding, load_holdings


TRADES_PATH = os.path.join("logs", "trades.json")
DAILY_REPORT_PATH = os.path.join("logs", "daily_report.csv")
DAILY_REPORT_STATE_PATH = os.path.join("logs", "daily_report_state.json")


@dataclass(frozen=True)
class TradeStats:
    completed_trades: int
    win_trades: int
    win_rate: float
    total_realized_pnl: float


def ensure_trades_file(path: str = TRADES_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def load_trades(path: str = TRADES_PATH) -> list[dict]:
    ensure_trades_file(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or []


def save_trades(trades: list[dict], path: str = TRADES_PATH) -> None:
    ensure_trades_file(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


def append_trade_record(record: dict, path: str = TRADES_PATH) -> None:
    trades = load_trades(path)
    trades.append(record)
    save_trades(trades, path)


def calc_trade_stats(trades: list[dict]) -> TradeStats:
    # Match SELL against average BUY cost by symbol
    cost_map: dict[str, dict] = {}
    completed = 0
    win = 0
    total_pnl = 0.0

    for t in trades:
        symbol = str(t.get("symbol", "")).upper()
        side = str(t.get("side", "")).upper()
        qty = int(float(t.get("qty", 0) or 0))
        price = float(t.get("price", 0) or 0)
        if not symbol or qty <= 0 or price <= 0:
            continue

        state = cost_map.setdefault(symbol, {"qty": 0, "cost": 0.0})
        if side == "BUY":
            new_qty = state["qty"] + qty
            new_cost = state["cost"] + (price * qty)
            state["qty"] = new_qty
            state["cost"] = new_cost
        elif side == "SELL":
            if state["qty"] <= 0:
                continue
            match_qty = min(qty, state["qty"])
            avg_cost = state["cost"] / state["qty"] if state["qty"] > 0 else 0.0
            pnl = (price - avg_cost) * match_qty
            total_pnl += pnl
            completed += 1
            if pnl > 0:
                win += 1
            state["qty"] -= match_qty
            state["cost"] -= avg_cost * match_qty

    win_rate = (win / completed * 100.0) if completed > 0 else 0.0
    return TradeStats(completed_trades=completed, win_trades=win, win_rate=win_rate, total_realized_pnl=total_pnl)


def _now_date_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_daily_report(path: str = DAILY_REPORT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "date_utc",
                "holding_count",
                "holding_symbols",
                "completed_trades",
                "win_rate_pct",
                "realized_pnl",
            ],
        )
        w.writeheader()


def write_daily_report(
    *,
    holdings: list[Holding] | None = None,
    trades: list[dict] | None = None,
    path: str = DAILY_REPORT_PATH,
) -> dict:
    holdings = holdings if holdings is not None else load_holdings()
    trades = trades if trades is not None else load_trades()
    stats = calc_trade_stats(trades)
    active = [h for h in holdings if int(h.qty) > 0]
    row = {
        "date_utc": _now_date_utc(),
        "holding_count": len(active),
        "holding_symbols": ",".join(sorted(h.symbol for h in active)),
        "completed_trades": stats.completed_trades,
        "win_rate_pct": f"{stats.win_rate:.2f}",
        "realized_pnl": f"{stats.total_realized_pnl:.2f}",
    }
    _ensure_daily_report(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "date_utc",
                "holding_count",
                "holding_symbols",
                "completed_trades",
                "win_rate_pct",
                "realized_pnl",
            ],
        )
        w.writerow(row)
    return row


def _load_daily_state(path: str = DAILY_REPORT_STATE_PATH) -> dict:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or {}


def _save_daily_state(state: dict, path: str = DAILY_REPORT_STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_send_daily_report_today() -> bool:
    state = _load_daily_state()
    today = _now_date_utc()
    return state.get("last_sent_date") != today


def mark_daily_report_sent() -> None:
    state = _load_daily_state()
    state["last_sent_date"] = _now_date_utc()
    _save_daily_state(state)

