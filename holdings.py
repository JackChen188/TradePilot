from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


HOLDINGS_PATH = os.path.join("logs", "positions.json")


@dataclass
class Holding:
    symbol: str
    qty: int
    buy_price: float
    buy_time: str
    updated_at: str
    highest_price_since_entry: float = 0.0
    trailing_armed: bool = False
    trailing_active: bool = False
    partial_take_profit_done: bool = False
    initial_qty: int = 0
    add_position_count: int = 0
    last_peak_update_utc: str = ""
    last_sell_time: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_holdings_file(path: str = HOLDINGS_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def load_holdings(path: str = HOLDINGS_PATH) -> list[Holding]:
    ensure_holdings_file(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f) or []
    out: list[Holding] = []
    for x in raw:
        x2 = dict(x)
        x2.setdefault("highest_price_since_entry", float(x2.get("buy_price", 0.0) or 0.0))
        x2.setdefault("trailing_armed", False)
        x2.setdefault("trailing_active", bool(x2.get("trailing_armed", False)))
        x2.setdefault("partial_take_profit_done", False)
        x2.setdefault("initial_qty", int(x2.get("qty", 0) or 0))
        x2.setdefault("add_position_count", 0)
        x2.setdefault("last_peak_update_utc", x2.get("updated_at", ""))
        x2.setdefault("last_sell_time", "")
        out.append(Holding(**x2))
    return out


def save_holdings(holdings: list[Holding], path: str = HOLDINGS_PATH) -> None:
    ensure_holdings_file(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in holdings], f, ensure_ascii=False, indent=2)


def get_holding(holdings: list[Holding], symbol: str) -> Holding | None:
    s = symbol.upper()
    for h in holdings:
        if h.symbol.upper() == s and int(h.qty) > 0:
            return h
    return None


def has_holding(holdings: list[Holding], symbol: str) -> bool:
    return get_holding(holdings, symbol) is not None


def apply_buy(
    holdings: list[Holding],
    *,
    symbol: str,
    qty: int,
    price: float,
) -> None:
    if qty <= 0:
        return
    now = _now_iso()
    h = get_holding(holdings, symbol)
    if h is None:
        holdings.append(
            Holding(
                symbol=symbol.upper(),
                qty=int(qty),
                buy_price=float(price),
                buy_time=now,
                updated_at=now,
                highest_price_since_entry=float(price),
                trailing_armed=False,
                trailing_active=False,
                partial_take_profit_done=False,
                initial_qty=int(qty),
                add_position_count=0,
                last_peak_update_utc=now,
                last_sell_time="",
            )
        )
        return

    total_qty = int(h.qty) + int(qty)
    weighted = (float(h.buy_price) * int(h.qty) + float(price) * int(qty)) / float(total_qty)
    h.qty = int(total_qty)
    h.buy_price = float(weighted)
    h.updated_at = now
    h.highest_price_since_entry = max(float(h.highest_price_since_entry or 0.0), float(price))
    h.trailing_armed = False
    h.trailing_active = False
    h.partial_take_profit_done = False
    if int(h.initial_qty) <= 0:
        h.initial_qty = int(h.qty)
    h.add_position_count = int(h.add_position_count) + 1
    h.last_peak_update_utc = now


def apply_sell(
    holdings: list[Holding],
    *,
    symbol: str,
    qty: int,
) -> None:
    if qty <= 0:
        return
    h = get_holding(holdings, symbol)
    if h is None:
        return
    h.qty = max(0, int(h.qty) - int(qty))
    h.updated_at = _now_iso()
    h.last_sell_time = h.updated_at
    if h.qty <= 0:
        h.trailing_armed = False
        h.trailing_active = False
        h.highest_price_since_entry = 0.0
        h.partial_take_profit_done = False
        h.initial_qty = 0
        h.add_position_count = 0
        h.last_peak_update_utc = h.updated_at


def update_peak_price(
    holdings: list[Holding],
    *,
    symbol: str,
    current_price: float,
    trailing_activate_pct: float,
) -> Holding | None:
    h = get_holding(holdings, symbol)
    if h is None:
        return None
    now = _now_iso()
    if float(current_price) > float(h.highest_price_since_entry or 0.0):
        h.highest_price_since_entry = float(current_price)
        h.last_peak_update_utc = now
    pnl_pct = ((float(current_price) - float(h.buy_price)) / float(h.buy_price) * 100.0) if h.buy_price > 0 else 0.0
    if pnl_pct >= float(trailing_activate_pct):
        h.trailing_armed = True
        h.trailing_active = True
    h.updated_at = now
    return h


def get_holding_any(holdings: list[Holding], symbol: str) -> Holding | None:
    s = symbol.upper()
    for h in holdings:
        if h.symbol.upper() == s:
            return h
    return None

