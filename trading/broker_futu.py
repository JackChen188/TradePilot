from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading.models import OrderPreview, OrderResult

log = logging.getLogger("tradepilot.broker.futu")


@dataclass(frozen=True)
class FutuBrokerConfig:
    host: str
    port: int
    trd_env: str  # SIMULATE / REAL
    allow_real_trading: bool


class FutuBroker:
    """
    US stock/ETF trading only.
    - Default SIMULATE
    - REAL requires allow_real_trading=True AND explicit env selection
    """

    def __init__(self, cfg: FutuBrokerConfig):
        self.cfg = cfg
        self.quote_ctx = None
        self.trade_ctx = None

    def connect(self) -> None:
        from futu import OpenQuoteContext, OpenUSTradeContext, TrdEnv

        if self.cfg.trd_env == "REAL" and not self.cfg.allow_real_trading:
            raise RuntimeError(
                "Real trading is blocked by config. "
                "To enable: set TP_TRADE_ENV=REAL and TP_ALLOW_REAL=YES"
            )

        self.quote_ctx = OpenQuoteContext(host=self.cfg.host, port=self.cfg.port)

        # Create trade context (still no order until user confirms YES)
        self.trade_ctx = OpenUSTradeContext(host=self.cfg.host, port=self.cfg.port)

        env = TrdEnv.SIMULATE if self.cfg.trd_env == "SIMULATE" else TrdEnv.REAL
        try:
            self.trade_ctx.set_trd_env(env)
        except Exception:
            # Some futu-api versions don't need set_trd_env; placing order will carry env parameter.
            pass

        log.info("Connected to FutuOpenD host=%s port=%s env=%s", self.cfg.host, self.cfg.port, self.cfg.trd_env)

    def close(self) -> None:
        if self.trade_ctx is not None:
            try:
                self.trade_ctx.close()
            except Exception:
                pass
        if self.quote_ctx is not None:
            try:
                self.quote_ctx.close()
            except Exception:
                pass

    def ensure_regular_session(self, codes: list[str]) -> None:
        """
        Block pre-market / after-hours by checking market state.
        This is a hard gate: if state isn't regular TRADING, we refuse to place orders.
        """
        from futu import MarketState, RET_OK

        ret, data = self.quote_ctx.get_market_state(codes)
        if ret != RET_OK:
            raise RuntimeError(f"get_market_state failed: {data}")
        if data is None or data.empty:
            raise RuntimeError("get_market_state returned empty")

        bad = []
        for _, row in data.iterrows():
            code = str(row.get("code"))
            state = row.get("market_state")
            if state != MarketState.TRADING:
                bad.append((code, state))

        if bad:
            raise RuntimeError(f"Blocked: not in regular session TRADING. market_state={bad}")

    def get_positions(self) -> pd.DataFrame:
        from futu import RET_OK, TrdEnv

        env = TrdEnv.SIMULATE if self.cfg.trd_env == "SIMULATE" else TrdEnv.REAL
        ret, data = self.trade_ctx.position_list_query(trd_env=env)
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query failed: {data}")
        return data if data is not None else pd.DataFrame()

    def place_order(self, preview: OrderPreview) -> OrderResult:
        from futu import OrderType, RET_OK, TrdEnv, TrdSide

        env = TrdEnv.SIMULATE if self.cfg.trd_env == "SIMULATE" else TrdEnv.REAL
        side = TrdSide.BUY if preview.side == "BUY" else TrdSide.SELL

        log.info("Placing order: %s %s qty=%s price=%.4f notional=%.2f",
                 preview.side, preview.code, preview.qty, preview.price, preview.notional_usd)

        ret, data = self.trade_ctx.place_order(
            price=preview.price,
            qty=preview.qty,
            code=preview.code,
            trd_side=side,
            order_type=OrderType.NORMAL,
            trd_env=env,
        )
        if ret != RET_OK:
            return OrderResult(
                code=preview.code,
                side=preview.side,
                qty=preview.qty,
                price=preview.price,
                trd_env=self.cfg.trd_env,
                ok=False,
                message=str(data),
            )

        order_id = None
        try:
            if isinstance(data, pd.DataFrame) and not data.empty and "order_id" in data.columns:
                order_id = str(data.iloc[0]["order_id"])
        except Exception:
            order_id = None

        return OrderResult(
            code=preview.code,
            side=preview.side,
            qty=preview.qty,
            price=preview.price,
            trd_env=self.cfg.trd_env,
            ok=True,
            message="ORDER_SENT",
            order_id=order_id,
        )

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        from futu import RET_OK, TrdEnv

        env = TrdEnv.SIMULATE if self.cfg.trd_env == "SIMULATE" else TrdEnv.REAL
        ret, data = self.trade_ctx.order_list_query(trd_env=env, order_id=order_id)
        if ret != RET_OK:
            return {"ok": False, "message": f"order_list_query failed: {data}"}
        if data is None or data.empty:
            return {"ok": False, "message": "order_list_query empty", "order_id": order_id}

        row = data.iloc[0].to_dict()
        row["ok"] = True
        return row

