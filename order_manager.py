from __future__ import annotations

from dataclasses import dataclass

from broker_futu import FutuLiveBroker
from indicators import Indicators


@dataclass(frozen=True)
class OrderRequest:
    code: str
    current_price: float
    limit_price: float
    qty: int
    est_amount: float
    score: int
    indicators: Indicators


class OrderManager:
    def __init__(self, broker: FutuLiveBroker):
        self.broker = broker

    def preview(self, req: OrderRequest) -> None:
        ind = req.indicators
        print("\n===== ORDER PREVIEW (REAL, LIMIT, BUY ONLY) =====")
        print(f"Stock code:   {req.code}")
        print(f"Current price:{req.current_price:.4f}")
        print(f"Limit price:  {req.limit_price:.4f}")
        print(f"Quantity:     {req.qty}")
        print(f"Est. amount:  ${req.est_amount:.2f}")
        print(f"Score:        {req.score}")
        print("Indicators:")
        print(f"  MA20:   {ind.ma20 if ind.ma20 is not None else 'NA'}")
        print(f"  MA60:   {ind.ma60 if ind.ma60 is not None else 'NA'}")
        print(f"  MA200:  {ind.ma200 if ind.ma200 is not None else 'NA'}")
        print(f"  RSI14:  {ind.rsi14 if ind.rsi14 is not None else 'NA'}")
        print(f"  ret5d%: {ind.ret5d_pct if ind.ret5d_pct is not None else 'NA'}")
        print("===============================================\n")

    def confirm_yes(self) -> None:
        ans = input("Type YES to place the order (anything else to abort): ").strip().upper()
        if ans != "YES":
            raise RuntimeError("Aborted by user.")

    def place_buy_with_manual_confirmation(self, req: OrderRequest):
        self.broker.ensure_us_stock_only(req.code)
        self.broker.ensure_regular_session(req.code)

        self.preview(req)
        self.confirm_yes()

        ok, data = self.broker.place_limit_buy(
            code=req.code,
            qty=req.qty,
            price=req.limit_price,
        )
        return ok, data

