from __future__ import annotations

import json
import os
import secrets
import string
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone


PENDING_PATH = os.path.join("logs", "pending_orders.json")


@dataclass
class PendingOrder:
    order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: float
    expire_time: str
    confirm_code: str
    status: str = "PENDING"
    created_at: str = ""
    message: str = ""
    broker_order_id: str = ""
    broker_status: str = ""
    updated_at: str = ""
    prompt_shown: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ensure_pending_file(path: str = PENDING_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)


def load_pending_orders(path: str = PENDING_PATH) -> list[PendingOrder]:
    ensure_pending_file(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f) or []
    out: list[PendingOrder] = []
    for x in raw:
        out.append(PendingOrder(**x))
    return out


def save_pending_orders(orders: list[PendingOrder], path: str = PENDING_PATH) -> None:
    ensure_pending_file(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in orders], f, ensure_ascii=False, indent=2)


def generate_confirm_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def create_pending_order(
    *,
    symbol: str,
    side: str,
    qty: int,
    limit_price: float,
    expires_in_seconds: int = 300,
) -> PendingOrder:
    now = _utc_now()
    oid = secrets.token_hex(8).upper()
    return PendingOrder(
        order_id=oid,
        symbol=symbol,
        side=side.upper(),
        qty=int(qty),
        limit_price=float(limit_price),
        expire_time=_iso(now + timedelta(seconds=int(expires_in_seconds))),
        confirm_code=generate_confirm_code(),
        status="PENDING",
        created_at=_iso(now),
        updated_at=_iso(now),
        prompt_shown=False,
    )


def expire_pending_orders(orders: list[PendingOrder], now: datetime | None = None) -> bool:
    changed = False
    now = now or _utc_now()
    for o in orders:
        if o.status == "PENDING" and _parse_iso(o.expire_time) < now:
            o.status = "EXPIRED"
            changed = True
    return changed


def find_pending_match(
    orders: list[PendingOrder],
    *,
    side: str,
    symbol: str,
    qty: int,
    confirm_code: str,
) -> PendingOrder | None:
    side_u = side.upper()
    s = symbol.upper()
    c = confirm_code.upper()
    for o in orders:
        if o.status != "PENDING":
            continue
        if (
            o.side.upper() == side_u
            and o.symbol.upper() == s
            and int(o.qty) == int(qty)
            and o.confirm_code.upper() == c
        ):
            return o
    return None


def has_recent_same_signal(
    orders: list[PendingOrder],
    *,
    symbol: str,
    side: str,
    cooldown_seconds: int,
    now: datetime | None = None,
) -> bool:
    now = now or _utc_now()
    symbol_u = symbol.upper()
    side_u = side.upper()
    for o in orders:
        if o.symbol.upper() != symbol_u or o.side.upper() != side_u:
            continue
        try:
            created = _parse_iso(o.created_at)
        except Exception:
            continue
        if (now - created).total_seconds() <= int(cooldown_seconds):
            return True
    return False

