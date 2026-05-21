from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Indicators:
    close: float
    ma20: float | None
    ma60: float | None
    ma100: float | None
    ma200: float | None
    rsi14: float | None
    ret5d_pct: float | None
    ret63_pct: float | None


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100 - (100 / (1 + rs))


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if "close" not in df.columns:
        raise ValueError("kline dataframe must contain 'close'")

    out = df.copy()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")

    out["ma20"] = out["close"].rolling(window=20, min_periods=20).mean()
    out["ma60"] = out["close"].rolling(window=60, min_periods=60).mean()
    out["ma100"] = out["close"].rolling(window=100, min_periods=100).mean()
    out["ma200"] = out["close"].rolling(window=200, min_periods=200).mean()

    out["rsi14"] = rsi_wilder(out["close"], period=14)

    # 5 trading-day return (pct)
    out["ret5d_pct"] = (out["close"] / out["close"].shift(5) - 1.0) * 100.0
    # ~3-month return (63 trading days)
    out["ret63_pct"] = (out["close"] / out["close"].shift(63) - 1.0) * 100.0
    return out


def latest_indicators(df_with_indicators: pd.DataFrame) -> Indicators:
    if df_with_indicators.empty:
        raise ValueError("Empty dataframe")
    last = df_with_indicators.iloc[-1]

    def _num(x):
        if x is None or (isinstance(x, float) and (math.isnan(x) or not math.isfinite(x))):
            return None
        try:
            v = float(x)
            if not math.isfinite(v):
                return None
            return v
        except Exception:
            return None

    close = float(last["close"])
    return Indicators(
        close=close,
        ma20=_num(last.get("ma20")),
        ma60=_num(last.get("ma60")),
        ma100=_num(last.get("ma100")),
        ma200=_num(last.get("ma200")),
        rsi14=_num(last.get("rsi14")),
        ret5d_pct=_num(last.get("ret5d_pct")),
        ret63_pct=_num(last.get("ret63_pct")),
    )

