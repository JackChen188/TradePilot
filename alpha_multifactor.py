"""
Quality + momentum factor helpers for expanding the Alpha candidate universe.

Notes
-----
- Futu OpenQuoteContext in this repo does not expose full financial statements;
  **quality** uses price-based proxies (lower short-term volatility + distance above MA200),
  documented in code. When you wire a vendor financial API, replace
  ``quality_proxy_from_prices`` with fundamentals-based scores.
- **Momentum** follows a classic 6-month / 21-day exclusion window on closes:
  ret = close[t-21] / close[t-126] - 1  (trading days, approx 1m / 6m).
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Iterable

import numpy as np
import pandas as pd


def momentum_6m_exclude_1m(close: pd.Series) -> float | None:
    """
    Past ~6 months return excluding the most recent ~1 month (21 trading days).

    Uses shift offsets on the **last** row of ``close``:
    ret = close.shift(21) / close.shift(126) - 1
    """
    if close is None or len(close) < 130:
        return None
    s = pd.to_numeric(close, errors="coerce")
    num = float(s.iloc[-21])
    den = float(s.iloc[-126])
    if den <= 0 or not math.isfinite(num) or not math.isfinite(den):
        return None
    return float(num / den - 1.0)


def _annualized_vol(close: pd.Series, window: int = 63) -> float | None:
    if close is None or len(close) < window + 2:
        return None
    s = pd.to_numeric(close, errors="coerce").iloc[-window:]
    lr = np.log(s / s.shift(1)).dropna()
    if len(lr) < max(20, window // 3):
        return None
    daily_vol = float(lr.std(ddof=1))
    return float(daily_vol * math.sqrt(252.0))


def quality_proxy_from_prices(close: pd.Series, *, ma200: float | None) -> float | None:
    """
    Proxy for "quality / stability" without fundamentals:
    - Prefer lower 63d realized vol (inverse used in z-scoring later).
    - Mild bonus when last close is above long MA200 (if provided).
    """
    vol = _annualized_vol(close, window=63)
    if vol is None or vol <= 0:
        return None
    last = float(pd.to_numeric(close, errors="coerce").iloc[-1])
    ma_bonus = 0.0
    if ma200 is not None and ma200 > 0 and last > float(ma200):
        ma_bonus = 0.15
    # Higher raw score = "better" before cross-sectional z-score.
    return float(ma_bonus + 1.0 / vol)


def zscore_series(values: dict[str, float | None]) -> dict[str, float]:
    xs = {k: float(v) for k, v in values.items() if v is not None and math.isfinite(float(v))}
    if len(xs) < 3:
        return {k: 0.0 for k in xs}
    arr = np.array(list(xs.values()), dtype=float)
    mu = float(arr.mean())
    sd = float(arr.std(ddof=1))
    if sd <= 1e-12:
        return {k: 0.0 for k in xs}
    return {k: float((float(xs[k]) - mu) / sd) for k in xs}


def composite_factor_score(
    *,
    momentum_raw: dict[str, float | None],
    quality_raw: dict[str, float | None],
    w_mom: float = 0.5,
    w_qual: float = 0.5,
) -> pd.DataFrame:
    """
    Standardize cross-sectionally (z-score) and combine.

    Returns columns: symbol, z_momentum, z_quality, composite
    """
    zm = zscore_series(momentum_raw)
    zq = zscore_series(quality_raw)
    symbols = sorted(set(zm) | set(zq))
    rows = []
    for sym in symbols:
        m = zm.get(sym, 0.0)
        q = zq.get(sym, 0.0)
        comp = float(w_mom) * m + float(w_qual) * q
        rows.append({"symbol": sym, "z_momentum": m, "z_quality": q, "composite": comp})
    return pd.DataFrame(rows).sort_values("composite", ascending=False).reset_index(drop=True)


def load_symbol_list_json(path: str, *, key: str = "symbols") -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        raw = data.get(key) or []
        return [str(x).strip().upper() for x in raw if str(x).strip()]
    except Exception:
        return []


def save_symbol_list_json(path: str, symbols: Iterable[str], *, meta: dict[str, Any] | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload: dict[str, Any] = {
        "symbols": sorted({str(s).strip().upper() for s in symbols if str(s).strip()}),
    }
    if meta:
        payload.update(meta)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def score_from_kline_df(df: pd.DataFrame) -> tuple[float | None, float | None]:
    """Return (momentum_6m_ex_1m, quality_proxy) from an indicator-enriched daily df."""
    if df is None or df.empty or "close" not in df.columns:
        return None, None
    close = df["close"]
    mom = momentum_6m_exclude_1m(close)
    ma200 = None
    if "ma200" in df.columns and pd.notna(df["ma200"].iloc[-1]):
        ma200 = float(df["ma200"].iloc[-1])
    qual = quality_proxy_from_prices(close, ma200=ma200)
    return mom, qual


def top_fraction_symbols(df_scores: pd.DataFrame, top_frac: float = 0.2) -> list[str]:
    if df_scores is None or df_scores.empty:
        return []
    n = max(1, int(math.ceil(len(df_scores) * float(top_frac))))
    return [str(x) for x in df_scores.head(n)["symbol"].tolist()]
