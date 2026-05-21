from __future__ import annotations

import csv
import os
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class OrderPlan:
    code: str
    side: str
    current_price: float
    limit_price: float
    qty: int
    est_amount: float


def build_buy_plan(
    *,
    code: str,
    current_price: float,
    limit_price_factor: float,
    max_order_usd: float,
    min_order_qty: int,
) -> OrderPlan | None:
    if not (current_price > 0) or not math.isfinite(current_price):
        raise ValueError(f"Invalid current_price for {code}: {current_price}")

    limit_price = float(current_price) * float(limit_price_factor)
    if not (limit_price > 0) or not math.isfinite(limit_price):
        raise ValueError(f"Invalid limit_price for {code}: {limit_price}")

    # New rule:
    # qty = max(int(MAX_ORDER_VALUE_USD / price), MIN_ORDER_QTY)
    qty = int(float(max_order_usd) // limit_price)
    qty = max(int(qty), int(min_order_qty))

    est = float(qty) * float(limit_price)
    # Safety: if MIN_ORDER_QTY forces est > max, refuse.
    if est > float(max_order_usd):
        return None

    return OrderPlan(
        code=code,
        side="BUY",
        current_price=float(current_price),
        limit_price=float(limit_price),
        qty=int(qty),
        est_amount=float(est),
    )


def build_sell_plan(
    *,
    code: str,
    current_price: float,
    limit_price_factor: float,
    max_order_usd: float,
    held_qty: int,
) -> OrderPlan | None:
    if held_qty <= 0:
        return None
    if not (current_price > 0) or not math.isfinite(current_price):
        raise ValueError(f"Invalid current_price for {code}: {current_price}")

    limit_price = float(current_price) * float(limit_price_factor)
    if not (limit_price > 0) or not math.isfinite(limit_price):
        raise ValueError(f"Invalid limit_price for {code}: {limit_price}")

    max_qty_by_notional = int(float(max_order_usd) // limit_price)
    qty = min(int(held_qty), int(max_qty_by_notional))
    if qty < 1:
        return None

    est = float(qty) * float(limit_price)
    return OrderPlan(
        code=code,
        side="SELL",
        current_price=float(current_price),
        limit_price=float(limit_price),
        qty=int(qty),
        est_amount=float(est),
    )


TRADE_LOG_PATH = os.path.join("logs", "trade_log.csv")

TRADE_LOG_FIELDS = [
    "ts_utc",
    "code",
    "score",
    "selected",
    "action",
    "current_price",
    "limit_price",
    "qty",
    "est_amount",
    "ma20",
    "ma60",
    "ma200",
    "rsi14",
    "ret5d_pct",
    "ret63_pct",
    "market_mode",
    "account_drawdown_pct",
    "candidate_rank",
    "order_ok",
    "order_id",
    "order_status",
    "message",
    "error",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_trade_log(path: str = TRADE_LOG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        w.writeheader()


def append_trade_log(row: dict[str, Any], path: str = TRADE_LOG_PATH) -> None:
    ensure_trade_log(path)
    row2 = {k: row.get(k, "") for k in TRADE_LOG_FIELDS}
    row2["ts_utc"] = row2.get("ts_utc") or _utc_now()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        w.writerow(row2)

