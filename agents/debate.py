from __future__ import annotations

from typing import Any


def _flagged(reports: dict[str, Any], name: str) -> bool:
    reports = reports or {}
    risk_flags = reports.get("risk_flags") or []
    return name in risk_flags


def run_bull_bear_review(ctx: dict[str, Any], reports: dict[str, Any]) -> dict[str, Any]:
    ctx = ctx or {}
    reports = reports or {}
    tech = reports.get("technical_report") or {}
    news = reports.get("news_report") or {}
    fundamental = reports.get("fundamental_report") or {}
    position = reports.get("position_report") or {}
    strategy = ctx.get("strategy") or {}
    portfolio = ctx.get("portfolio") or {}
    order_book = ctx.get("order_book") or {}

    bull: list[str] = []
    bear: list[str] = []
    risk_flags: list[str] = []

    if int(strategy.get("score", 0) or 0) >= 80:
        bull.append("deterministic score is strong")
    if float(strategy.get("rank_pct", 0.0) or 0.0) >= 0.70:
        bull.append("relative strength is in the top cohort")
    if tech.get("trend") == "bullish":
        bull.append("technical trend is aligned")
    if fundamental.get("tone") == "supportive":
        bull.append("fundamentals support sizing")
    if news.get("verdict") == "利好":
        bull.append("recent news keywords lean bullish")

    if tech.get("trend") == "bearish":
        bear.append("trend evidence conflicts with a new long")
        risk_flags.append("trend_conflict")
    if fundamental.get("tone") == "penalty":
        bear.append("fundamental score penalizes the candidate")
        risk_flags.append("fundamental_penalty")
    if news.get("verdict") == "利空":
        bear.append("recent news keywords lean bearish")
        risk_flags.append("news_bearish")
    if bool(position.get("has_position")):
        bear.append("there is already existing exposure in this symbol")
        risk_flags.append("existing_position")
    if str(portfolio.get("market_mode", "")).upper() == "DEFENSE":
        bear.append("market mode is defensive")
        risk_flags.append("defense_market")
    if bool(portfolio.get("buy_paused", False)):
        bear.append("risk state has paused new buys")
        risk_flags.append("buy_pause")
    if float(portfolio.get("available_cash", 0.0) or 0.0) < float(ctx.get("current_price", 0.0) or 0.0):
        bear.append("available USD cash is below one share at current price")
        risk_flags.append("insufficient_cash")
    if bool(order_book.get("available")):
        spread = float(order_book.get("spread", 0.0) or 0.0)
        price = float(ctx.get("current_price", 0.0) or 0.0)
        imbalance = float(order_book.get("imbalance", 0.0) or 0.0)
        if price > 0 and spread / price > 0.003:
            bear.append("order book spread is wide for a limit entry")
            risk_flags.append("wide_spread")
        if imbalance < -0.35:
            bear.append("order book ask pressure is materially heavier than bids")
            risk_flags.append("ask_pressure")

    confidence_delta = min(20, len(bull) * 4) - min(25, len(set(risk_flags)) * 5)
    return {
        "bull_case": bull[:8],
        "bear_case": bear[:8],
        "risk_flags": sorted(set(risk_flags)),
        "confidence_delta": int(confidence_delta),
    }
