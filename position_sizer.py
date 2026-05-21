from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionConstraints:
    max_trade_cash_pct: float = 0.20
    max_symbol_asset_pct: float = 0.40
    max_total_asset_pct: float = 0.90
    max_holding_count: int = 3


@dataclass(frozen=True)
class PositionSizingResult:
    allowed: bool
    qty: int
    limit_price: float
    trade_budget: float
    applied_multiplier: float
    reason: str


def calc_buy_qty(
    *,
    available_cash: float,
    total_assets: float,
    current_symbol_value: float,
    current_total_exposure_value: float,
    current_holding_count: int,
    has_existing_position: bool,
    limit_price: float,
    max_order_value_usd: float,
    constraints: PositionConstraints = PositionConstraints(),
    volatility_multiplier: float = 1.0,
) -> PositionSizingResult:
    if has_existing_position:
        return PositionSizingResult(False, 0, limit_price, 0.0, float(volatility_multiplier), "existing position; duplicate buy blocked")
    if current_holding_count >= constraints.max_holding_count:
        return PositionSizingResult(False, 0, limit_price, 0.0, float(volatility_multiplier), "max holding count reached")
    if limit_price <= 0:
        return PositionSizingResult(False, 0, limit_price, 0.0, float(volatility_multiplier), "invalid limit price")

    trade_budget = min(float(available_cash) * constraints.max_trade_cash_pct, float(max_order_value_usd))
    trade_budget = trade_budget * max(float(volatility_multiplier), 0.0)
    if trade_budget <= 0:
        return PositionSizingResult(False, 0, limit_price, 0.0, float(volatility_multiplier), "trade budget <= 0")

    # Position cap: symbol position <= total assets * 30%
    symbol_cap = float(total_assets) * constraints.max_symbol_asset_pct
    remain_cap = symbol_cap - float(current_symbol_value)
    if remain_cap <= 0:
        return PositionSizingResult(False, 0, limit_price, trade_budget, float(volatility_multiplier), "symbol position cap reached")

    total_cap = float(total_assets) * constraints.max_total_asset_pct
    remain_total_cap = total_cap - float(current_total_exposure_value)
    if remain_total_cap <= 0:
        return PositionSizingResult(False, 0, limit_price, trade_budget, float(volatility_multiplier), "total exposure cap reached")

    budget = min(trade_budget, remain_cap, remain_total_cap, float(available_cash))
    qty = int(budget // float(limit_price))
    if qty < 1:
        return PositionSizingResult(False, 0, limit_price, trade_budget, float(volatility_multiplier), "qty<1 under budget/cap")

    reason = "ok"
    if float(volatility_multiplier) < 1.0:
        reason = f"ok (volatility multiplier={float(volatility_multiplier):.2f})"
    return PositionSizingResult(True, qty, limit_price, trade_budget, float(volatility_multiplier), reason)

