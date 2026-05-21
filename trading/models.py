from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OrderPreview:
    code: str
    side: Side
    price: float
    qty: int
    notional_usd: float
    reason: str


@dataclass(frozen=True)
class OrderResult:
    code: str
    side: Side
    qty: int
    price: float
    trd_env: str
    ok: bool
    message: str
    order_id: str | None = None

