from __future__ import annotations

from typing import Any


def run_portfolio_manager(ctx: dict[str, Any], debate: dict[str, Any]) -> dict[str, Any]:
    ctx = ctx or {}
    debate = debate or {}
    strategy = ctx.get("strategy") or {}
    signal = str(strategy.get("signal", "HOLD")).upper()
    score = int(strategy.get("score", 0) or 0)
    flags = set(debate.get("risk_flags") or [])
    hard_flags = {"defense_market", "buy_pause", "news_bearish", "insufficient_cash"}

    if signal == "SELL":
        final_action = "APPROVE_SELL"
    elif signal == "BUY":
        if flags & hard_flags:
            final_action = "REJECT_BUY"
        elif flags:
            final_action = "DOWNGRADE_TO_HOLD"
        else:
            final_action = "APPROVE_BUY"
    else:
        final_action = "HOLD"

    confidence = max(0, min(100, score + int(debate.get("confidence_delta", 0) or 0)))
    return {
        "final_action": final_action,
        "confidence": confidence,
        "rationale": (
            f"strategy_signal={signal}; score={score}; "
            f"confidence_delta={debate.get('confidence_delta', 0)}; "
            f"risk_flags={','.join(sorted(flags)) or 'none'}"
        ),
    }


def action_to_review_decision(action: str) -> str:
    action = str(action or "").upper()
    if action == "APPROVE_BUY":
        return "APPROVE_BUY_REVIEW"
    if action == "DOWNGRADE_TO_HOLD":
        return "DOWNGRADE_TO_HOLD_REVIEW"
    if action == "REJECT_BUY":
        return "DOWNGRADE_TO_HOLD_REVIEW"
    if action == "APPROVE_SELL":
        return "APPROVE_SELL_REVIEW"
    return "WATCHLIST_REVIEW"
