from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    allowed_symbols: tuple[str, ...]
    max_notional_usd: float = 200.0


def validate_symbol_allowed(code: str, allowed: tuple[str, ...]) -> None:
    if code not in allowed:
        raise ValueError(f"Symbol not allowed: {code}. Allowed: {allowed}")

    # Hard block options-like codes (defensive). US equity/ETF codes are simple like US.AAPL
    if any(ch.isdigit() for ch in code.split(".")[-1]) and code not in allowed:
        raise ValueError(f"Potential option code blocked: {code}")


def calc_qty_by_notional(price: float, max_notional: float) -> int:
    if not math.isfinite(price) or price <= 0:
        return 0
    return int(max_notional // price)


def clamp_qty_by_notional(price: float, qty: int, max_notional: float) -> int:
    if qty <= 0:
        return 0
    max_qty = calc_qty_by_notional(price, max_notional)
    return min(qty, max_qty)

