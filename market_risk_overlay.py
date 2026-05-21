"""
Optional risk overlay: VIX + VOO vs MA200 + sentiment stub.

**Conflict rule with TradePilot live logic:** SPY/VOO regime from ``resolve_market_mode``
remains authoritative for ATTACK/DEFENSE. This module only returns *adjustments* applied
when ``market_mode == "ATTACK"`` (e.g. stricter rank threshold), never forcing buys in DEFENSE.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Literal
from urllib.request import Request, urlopen

import pandas as pd

RiskTone = Literal["NORMAL", "CAUTIOUS", "CAPITAL_PRESERVATION"]


@dataclass(frozen=True)
class RiskOverlayAdjustments:
    tone: RiskTone
    rank_top_pct_bump: float
    message: str


def _http_json(url: str, *, timeout_sec: float = 6.0) -> Any:
    req = Request(url, headers={"User-Agent": "TradePilot/1.0"})
    with urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def fetch_vix_last_close_yahoo() -> float | None:
    """
    Best-effort VIX last close via Yahoo chart API (no extra pip deps).
    Returns None on any failure.
    """
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=10d&interval=1d"
    try:
        data = _http_json(url)
        res = data.get("chart", {}).get("result", [])
        if not res:
            return None
        closes = res[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [float(x) for x in closes if x is not None and math.isfinite(float(x))]
        if not closes:
            return None
        return float(closes[-1])
    except Exception:
        return None


def voo_below_ma200(voo_df: pd.DataFrame) -> bool | None:
    if voo_df is None or voo_df.empty or "close" not in voo_df.columns:
        return None
    last = voo_df.iloc[-1]
    c = last.get("close")
    m = last.get("ma200")
    try:
        if c is None or m is None or pd.isna(c) or pd.isna(m):
            return None
        return float(c) < float(m)
    except Exception:
        return None


def fetch_sentiment_stub() -> float | None:
    """
    Placeholder for external sentiment API.

    Set env ``TP_SENTIMENT_JSON_URL`` to an HTTPS endpoint returning JSON like:
    ``{"score": 0.12}`` in [-1, 1]. If unset or request fails, returns None (ignored).
    """
    url = (os.getenv("TP_SENTIMENT_JSON_URL") or "").strip()
    if not url:
        return None
    try:
        data = _http_json(url)
        if isinstance(data, dict) and "score" in data:
            return float(data["score"])
        if isinstance(data, (int, float)):
            return float(data)
    except Exception:
        return None
    return None


def classify_tone(
    *,
    vix: float | None,
    vix_cautious: float,
    vix_capital: float,
    voo_weak: bool | None,
    sentiment: float | None,
    sentiment_cautious: float,
    sentiment_capital: float,
) -> RiskTone:
    """
    Conservative heuristic: any single strong stress -> CAUTIOUS; two+ -> CAPITAL_PRESERVATION.
    """
    flags = 0
    if vix is not None and vix >= float(vix_capital):
        flags += 2
    elif vix is not None and vix >= float(vix_cautious):
        flags += 1

    if voo_weak is True:
        flags += 1
    elif voo_weak is None:
        pass

    if sentiment is not None:
        if sentiment <= float(sentiment_capital):
            flags += 2
        elif sentiment <= float(sentiment_cautious):
            flags += 1

    if flags >= 2:
        return "CAPITAL_PRESERVATION"
    if flags >= 1:
        return "CAUTIOUS"
    return "NORMAL"


def build_overlay(
    *,
    voo_df: pd.DataFrame | None,
    vix_cautious: float,
    vix_capital: float,
    sentiment_cautious: float,
    sentiment_capital: float,
    bump_cautious: float,
    bump_capital: float,
) -> RiskOverlayAdjustments:
    vix = fetch_vix_last_close_yahoo()
    voo_weak = voo_below_ma200(voo_df) if voo_df is not None else None
    sent = fetch_sentiment_stub()
    tone = classify_tone(
        vix=vix,
        vix_cautious=vix_cautious,
        vix_capital=vix_capital,
        voo_weak=voo_weak,
        sentiment=sent,
        sentiment_cautious=sentiment_cautious,
        sentiment_capital=sentiment_capital,
    )
    if tone == "CAPITAL_PRESERVATION":
        bump = float(bump_capital)
    elif tone == "CAUTIOUS":
        bump = float(bump_cautious)
    else:
        bump = 0.0
    parts = [f"tone={tone}", f"vix={vix}", f"voo_below_ma200={voo_weak}", f"sentiment={sent}"]
    return RiskOverlayAdjustments(tone=tone, rank_top_pct_bump=bump, message="; ".join(parts))
