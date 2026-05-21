from __future__ import annotations

import logging
import re
from decimal import ROUND_DOWN, Decimal
from dataclasses import dataclass
from typing import Any

import pandas as pd

log = logging.getLogger("tradepilot.live.broker_futu")


US_STOCK_CODE_RE = re.compile(r"^US\.[A-Z0-9]+$", re.IGNORECASE)


@dataclass(frozen=True)
class FutuLiveBrokerConfig:
    host: str
    port: int
    max_single_notional_usd: float


class FutuLiveBroker:
    """
    Hard-guardrailed manual-confirm trading broker for Futu OpenAPI.

    Constraints enforced:
    - REAL trading only (TrdEnv.REAL)
    - US stock only (no options)
    - Allow extended hours (pre/after) by request; only block CLOSED
    """

    def __init__(self, cfg: FutuLiveBrokerConfig):
        self.cfg = cfg
        self.quote_ctx = None
        self.trade_ctx = None
        self.acc_id: int | None = None

    def connect(self) -> None:
        from futu import OpenQuoteContext, OpenSecTradeContext, TrdMarket

        self.quote_ctx = OpenQuoteContext(host=self.cfg.host, port=self.cfg.port)
        # Newer futu-api uses OpenSecTradeContext and selects market by filter_trdmarket.
        self.trade_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=self.cfg.host,
            port=self.cfg.port,
        )
        self.acc_id = self._pick_us_real_account_id()

        log.info("Connected. host=%s port=%s acc_id=%s", self.cfg.host, self.cfg.port, self.acc_id)

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

    def _pick_us_real_account_id(self) -> int:
        from futu import RET_OK, TrdEnv

        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"get_acc_list failed: {data}")
        if data is None or data.empty:
            raise RuntimeError("get_acc_list returned empty")

        candidates: list[int] = []
        seen_types: set[str] = set()
        for _, row in data.iterrows():
            try:
                seen_types.add(str(row.get("acc_type")))
                if row.get("trd_env") != TrdEnv.REAL:
                    continue
                candidates.append(int(row.get("acc_id")))
            except Exception:
                continue

        if not candidates:
            raise RuntimeError(
                "No US REAL trading account found in OpenD. "
                f"Accounts detected acc_type={sorted(seen_types)}"
            )
        return int(candidates[0])

    def ensure_us_stock_only(self, code: str) -> None:
        from futu import Market, RET_OK, SecurityType

        if not US_STOCK_CODE_RE.fullmatch(code or ""):
            raise ValueError(f"Only US stock codes allowed (e.g. US.AAPL). Got: {code}")

        # Must be US STOCK (no options).
        ret, df = self.quote_ctx.get_stock_basicinfo(market=Market.US, stock_type=SecurityType.STOCK)
        if ret != RET_OK:
            raise RuntimeError(f"get_stock_basicinfo failed: {df}")
        if df is None or df.empty:
            raise RuntimeError("get_stock_basicinfo returned empty")

        if "code" not in df.columns:
            raise RuntimeError("get_stock_basicinfo missing 'code' column")

        if code not in set(str(x) for x in df["code"].tolist()):
            raise ValueError(f"Only US STOCK allowed; code not found in US STOCK list: {code}")

    def ensure_regular_session(self, code: str) -> None:
        """
        Allow pre/after hours; only block market CLOSED.
        """
        from futu import MarketState, RET_OK

        ret, data = self.quote_ctx.get_market_state([code])
        if ret != RET_OK:
            raise RuntimeError(f"get_market_state failed: {data}")
        if data is None or data.empty:
            raise RuntimeError("get_market_state returned empty")

        state = data.iloc[0].get("market_state")
        if state == getattr(MarketState, "CLOSED", None):
            raise RuntimeError(f"Blocked: market CLOSED. code={code} market_state={state}")
        if state not in {getattr(MarketState, "MORNING", None), getattr(MarketState, "AFTERNOON", None)}:
            log.warning("Extended-hours order allowed. code=%s market_state=%s", code, state)

    def place_limit_buy(self, *, code: str, qty: int, price: float) -> tuple[bool, Any]:
        return self.place_limit_order(code=code, side="BUY", qty=qty, price=price)

    def place_limit_order(self, *, code: str, side: str, qty: int, price: float) -> tuple[bool, Any]:
        """
        Places a NORMAL limit order in REAL environment.
        Returns (ok, data/error).
        """
        from futu import OrderType, RET_OK, TrdEnv, TrdSide

        if self.acc_id is None:
            raise RuntimeError("acc_id not set; did you call connect()?")

        side_u = side.strip().upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(f"Unsupported side: {side}. Must be BUY/SELL")
        if qty <= 0:
            raise ValueError("qty must be > 0")
        if not (price > 0):
            raise ValueError("price must be > 0")

        aligned_price = self._normalize_limit_price_for_buy(code=code, price=float(price))
        if aligned_price != float(price):
            log.info("Adjusted limit price to valid tick. code=%s raw=%.6f aligned=%.6f", code, price, aligned_price)

        notional = float(aligned_price) * int(qty)
        if notional > float(self.cfg.max_single_notional_usd):
            raise RuntimeError(
                f"Blocked: single order notional ${notional:.2f} exceeds limit ${self.cfg.max_single_notional_usd:.2f}"
            )

        trd_side = TrdSide.BUY if side_u == "BUY" else TrdSide.SELL
        ret, data = self.trade_ctx.place_order(
            price=float(aligned_price),
            qty=int(qty),
            code=code,
            trd_side=trd_side,
            order_type=OrderType.NORMAL,
            trd_env=TrdEnv.REAL,
            acc_id=int(self.acc_id),
        )
        if ret != RET_OK:
            return False, data
        return True, data

    def get_positions(self) -> pd.DataFrame:
        from futu import RET_OK, TrdEnv

        if self.acc_id is None:
            raise RuntimeError("acc_id not set; did you call connect()?")

        ret, data = self.trade_ctx.position_list_query(
            trd_env=TrdEnv.REAL,
            acc_id=int(self.acc_id),
        )
        if ret != RET_OK:
            raise RuntimeError(f"position_list_query failed: {data}")
        return data if data is not None else pd.DataFrame()

    def get_position_qty(self, code: str) -> int:
        df = self.get_positions()
        if df is None or df.empty:
            return 0
        for _, row in df.iterrows():
            if str(row.get("code", "")).upper() == code.upper():
                try:
                    return int(float(row.get("qty", 0)))
                except Exception:
                    return 0
        return 0

    def get_available_cash(self) -> float:
        from futu import RET_OK, TrdEnv

        if self.acc_id is None:
            raise RuntimeError("acc_id not set; did you call connect()?")

        ret, data = self.trade_ctx.accinfo_query(
            trd_env=TrdEnv.REAL,
            acc_id=int(self.acc_id),
        )
        if ret != RET_OK:
            raise RuntimeError(f"accinfo_query failed: {data}")
        if data is None or data.empty:
            raise RuntimeError("accinfo_query returned empty")

        row = data.iloc[0]
        base_ccy = str(row.get("currency") or "").upper()
        if base_ccy and base_ccy != "USD":
            log.info("Account base currency is %s, but TradePilot enforces USD-only cash.", base_ccy)
        # Enforce USD-only account values for US trading.
        # Do NOT fallback to generic cash fields (often HKD/CNY aggregated values).
        for col in ("us_cash", "usd_cash", "usd_net_cash_power"):
            if col in data.columns:
                try:
                    v = float(row.get(col))
                    if v >= 0:
                        log.info("Using USD cash field '%s'=%.4f for available cash", col, v)
                        return v
                except Exception:
                    continue
        raise RuntimeError(
            "Cannot determine USD available cash. "
            f"Need one of ['us_cash','usd_cash','usd_net_cash_power']; columns={list(data.columns)}"
        )

    def get_total_assets(self) -> float:
        from futu import RET_OK, TrdEnv

        if self.acc_id is None:
            raise RuntimeError("acc_id not set; did you call connect()?")

        ret, data = self.trade_ctx.accinfo_query(
            trd_env=TrdEnv.REAL,
            acc_id=int(self.acc_id),
        )
        if ret != RET_OK:
            raise RuntimeError(f"accinfo_query failed: {data}")
        if data is None or data.empty:
            raise RuntimeError("accinfo_query returned empty")

        row = data.iloc[0]
        base_ccy = str(row.get("currency") or "").upper()
        if base_ccy and base_ccy != "USD":
            log.info("Account base currency is %s, but TradePilot enforces USD-only assets.", base_ccy)
        # Enforce USD-only asset values for US trading.
        # Avoid generic total_assets/power fields, which may be non-USD.
        for col in ("usd_assets", "us_assets"):
            if col in data.columns:
                try:
                    v = float(row.get(col))
                    if v > 0:
                        log.info("Using USD asset field '%s'=%.4f for total assets", col, v)
                        return v
                except Exception:
                    continue
        # Fallback remains USD-only cash.
        return self.get_available_cash()

    def _normalize_limit_price_for_buy(self, *, code: str, price: float) -> float:
        spread = self._get_price_spread(code=code, fallback_price=price)
        d_price = Decimal(str(price))
        d_spread = Decimal(str(spread))
        if d_spread <= 0:
            raise RuntimeError(f"Invalid price spread for {code}: {spread}")

        # BUY limit should not exceed intended price, so use floor to nearest tick.
        units = (d_price / d_spread).to_integral_value(rounding=ROUND_DOWN)
        aligned = units * d_spread
        if aligned <= 0:
            raise RuntimeError(f"Aligned price <= 0 for {code}: raw={price}, spread={spread}")

        places = max(0, -d_spread.normalize().as_tuple().exponent)
        return float(round(float(aligned), places))

    def _get_price_spread(self, *, code: str, fallback_price: float) -> float:
        from futu import RET_OK

        ret, data = self.quote_ctx.get_market_snapshot([code])
        if ret == RET_OK and data is not None and not data.empty and "price_spread" in data.columns:
            try:
                spread = float(data.iloc[0]["price_spread"])
                if spread > 0:
                    return spread
            except Exception:
                pass

        # Fallback (US common tick): <1 => 0.001, >=1 => 0.01
        return 0.001 if float(fallback_price) < 1 else 0.01

    def query_order(self, order_id: str) -> pd.DataFrame:
        from futu import RET_OK, TrdEnv

        if self.acc_id is None:
            raise RuntimeError("acc_id not set; did you call connect()?")

        ret, data = self.trade_ctx.order_list_query(
            trd_env=TrdEnv.REAL,
            acc_id=int(self.acc_id),
            order_id=order_id,
        )
        if ret != RET_OK:
            raise RuntimeError(f"order_list_query failed: {data}")
        return data if data is not None else pd.DataFrame()

