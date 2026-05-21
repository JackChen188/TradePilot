from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from pending_orders import PendingOrder, save_pending_orders


SIGNAL_CACHE_PATH = os.path.join("logs", "signal_cache.json")


@dataclass
class SignalCacheItem:
    symbol: str
    side: str
    ts_utc: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ensure_signal_cache(path: str = SIGNAL_CACHE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def load_signal_cache(path: str = SIGNAL_CACHE_PATH) -> list[SignalCacheItem]:
    ensure_signal_cache(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f) or []
    return [SignalCacheItem(**x) for x in raw]


def save_signal_cache(items: list[SignalCacheItem], path: str = SIGNAL_CACHE_PATH) -> None:
    ensure_signal_cache(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in items], f, ensure_ascii=False, indent=2)


def is_in_cooldown(
    items: list[SignalCacheItem],
    *,
    symbol: str,
    side: str,
    cooldown_seconds: int,
) -> bool:
    now = datetime.now(timezone.utc)
    s = symbol.upper()
    d = side.upper()
    for it in items:
        if it.symbol.upper() == s and it.side.upper() == d:
            try:
                if (now - _parse_iso(it.ts_utc)).total_seconds() <= int(cooldown_seconds):
                    return True
            except Exception:
                continue
    return False


def mark_signal(items: list[SignalCacheItem], *, symbol: str, side: str) -> None:
    items.append(SignalCacheItem(symbol=symbol.upper(), side=side.upper(), ts_utc=_now_iso()))
    # keep recent history only
    if len(items) > 5000:
        del items[:-5000]


def has_pending_same_side(pending_orders: list[PendingOrder], *, symbol: str, side: str) -> bool:
    s = symbol.upper()
    d = side.upper()
    for o in pending_orders:
        if o.status == "PENDING" and o.symbol.upper() == s and o.side.upper() == d:
            return True
    return False


def _normalize_broker_status(v: str) -> str:
    x = (v or "").upper()
    if "CANCEL" in x:
        return "CANCELLED"
    if "FILLED" in x or "DEALT" in x:
        return "EXECUTED"
    if "FAIL" in x or "REJECT" in x:
        return "FAILED"
    return "SUBMITTED"


def update_submitted_orders_status(pending_orders: list[PendingOrder], broker) -> bool:
    """
    Update status for submitted orders by querying broker order status.
    """
    changed = False
    for o in pending_orders:
        if o.status not in ("SUBMITTED", "PENDING") or not o.broker_order_id:
            continue
        try:
            df = broker.query_order(o.broker_order_id)
            if df is None or df.empty:
                continue
            row = df.iloc[0].to_dict()
            bs = str(row.get("order_status", ""))
            new_status = _normalize_broker_status(bs)
            if new_status != o.status or bs != o.broker_status:
                o.status = new_status
                o.broker_status = bs
                o.updated_at = _now_iso()
                changed = True
        except Exception:
            continue
    if changed:
        save_pending_orders(pending_orders)
    return changed

