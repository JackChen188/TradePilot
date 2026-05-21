from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone


RISK_STATE_PATH = os.path.join("logs", "risk_state.json")


@dataclass
class RiskState:
    equity_peak: float
    current_equity: float
    drawdown_pct: float
    consecutive_losses: int
    pause_buy_until: str
    pause_reason: str
    updated_at: str
    processed_closed_trades: int = 0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _default_state(equity_base_usd: float) -> RiskState:
    now = _iso(_now_utc())
    base = float(max(equity_base_usd, 1.0))
    return RiskState(
        equity_peak=base,
        current_equity=base,
        drawdown_pct=0.0,
        consecutive_losses=0,
        pause_buy_until="",
        pause_reason="",
        updated_at=now,
        processed_closed_trades=0,
    )


def ensure_risk_state_file(path: str = RISK_STATE_PATH, *, equity_base_usd: float = 10_000.0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    state = _default_state(equity_base_usd)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)


def load_risk_state(path: str = RISK_STATE_PATH, *, equity_base_usd: float = 10_000.0) -> RiskState:
    ensure_risk_state_file(path, equity_base_usd=equity_base_usd)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f) or {}
    d = asdict(_default_state(equity_base_usd))
    d.update(raw)
    return RiskState(**d)


def save_risk_state(state: RiskState, path: str = RISK_STATE_PATH) -> None:
    ensure_risk_state_file(path, equity_base_usd=max(state.current_equity, 1.0))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)


def evaluate_buy_pause(
    *,
    trades: list[dict],
    state: RiskState,
    equity_base_usd: float,
    max_drawdown_pct: float,
    max_consecutive_loss: int,
    pause_days: int,
) -> RiskState:
    equity = float(max(equity_base_usd, 1.0))
    peak = equity
    loss_streak = 0
    closed = 0
    inv: dict[str, dict[str, float]] = {}
    last_pause_reason = state.pause_reason if state.pause_reason else ""
    pause_until_dt = _parse_iso(state.pause_buy_until)

    for t in trades:
        side = str(t.get("side", "")).upper()
        symbol = str(t.get("symbol", "")).upper()
        qty = int(float(t.get("qty", 0) or 0))
        price = float(t.get("price", 0) or 0)
        if not symbol or qty <= 0 or price <= 0:
            continue
        s = inv.setdefault(symbol, {"qty": 0, "cost": 0.0})
        if side == "BUY":
            s["qty"] += qty
            s["cost"] += qty * price
            continue
        if side != "SELL" or s["qty"] <= 0:
            continue

        m = min(qty, int(s["qty"]))
        avg = s["cost"] / s["qty"] if s["qty"] > 0 else 0.0
        pnl = (price - avg) * m
        s["qty"] -= m
        s["cost"] -= avg * m
        closed += 1

        equity += pnl
        peak = max(peak, equity)
        dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
        if pnl < 0:
            loss_streak += 1
        else:
            loss_streak = 0

        now = _now_utc()
        trigger_reason = ""
        if dd_pct >= float(max_drawdown_pct):
            trigger_reason = f"drawdown>{max_drawdown_pct:.1f}%"
        elif loss_streak >= int(max_consecutive_loss):
            trigger_reason = f"loss_streak>={max_consecutive_loss}"

        if trigger_reason:
            candidate = now + timedelta(days=int(max(pause_days, 1)))
            if pause_until_dt is None or candidate > pause_until_dt:
                pause_until_dt = candidate
            last_pause_reason = trigger_reason

    drawdown_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
    return RiskState(
        equity_peak=float(peak),
        current_equity=float(equity),
        drawdown_pct=float(drawdown_pct),
        consecutive_losses=int(loss_streak),
        pause_buy_until=_iso(pause_until_dt) if pause_until_dt else "",
        pause_reason=last_pause_reason,
        updated_at=_iso(_now_utc()),
        processed_closed_trades=int(closed),
    )


def is_buy_paused(state: RiskState, *, now_utc: datetime | None = None) -> bool:
    if not state.pause_buy_until:
        return False
    now = now_utc or _now_utc()
    until = _parse_iso(state.pause_buy_until)
    if until is None:
        return False
    return now < until
