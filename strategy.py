from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import Indicators

Signal = Literal["BUY", "SELL", "HOLD"]
MarketMode = Literal["ATTACK", "DEFENSE"]


@dataclass(frozen=True)
class AnalysisResult:
    code: str
    score: int
    signal: Signal
    indicators: Indicators
    reason: str
    market_mode: MarketMode = "ATTACK"


def resolve_market_mode(regime_ind: Indicators) -> MarketMode:
    if regime_ind.ma200 is not None and regime_ind.close > regime_ind.ma200:
        return "ATTACK"
    return "DEFENSE"


def score_buy(
    ind: Indicators,
    *,
    market_ok: bool,
    rank_top: bool,
    strong_exception: bool = False,
    rsi_upper: float = 68.0,
    ignore_rsi: bool = False,
) -> tuple[int, list[str]]:
    score = 0
    parts: list[str] = []

    if market_ok:
        score += 20
        parts.append("SPY>MA200(+20)")

    if ind.ma200 is not None and ind.close > ind.ma200:
        score += 20
        parts.append("price>MA200(+20)")

    if ind.ma20 is not None and ind.ma60 is not None and ind.ma20 > ind.ma60:
        score += 20
        parts.append("MA20>MA60(+20)")

    if rank_top:
        score += 20
        parts.append("ret63 rank top(+20)")

    if ignore_rsi:
        score += 10
        parts.append("RSI bypass(+10)")
    elif ind.rsi14 is not None and 45 <= ind.rsi14 <= float(rsi_upper):
        score += 10
        parts.append(f"RSI14 in[45,{float(rsi_upper):.0f}](+10)")
    elif strong_exception and ind.rsi14 is not None and ind.rsi14 <= float(rsi_upper):
        score += 10
        parts.append(f"RSI14 strong-exception<={float(rsi_upper):.0f}(+10)")

    if ind.ret5d_pct is not None and (-6.0 <= ind.ret5d_pct <= 4.0):
        score += 10
        parts.append("ret5d in[-6%,+4%](+10)")

    return score, parts


def analyze(
    code: str,
    ind: Indicators,
    *,
    buy_threshold: int = 60,
    market_ok: bool = True,
    rank_top: bool = False,
    market_mode: MarketMode = "ATTACK",
    strong_exception: bool = False,
    rsi_upper: float = 68.0,
    ignore_rsi: bool = False,
) -> AnalysisResult:
    score, parts = score_buy(
        ind,
        market_ok=market_ok,
        rank_top=rank_top,
        strong_exception=strong_exception,
        rsi_upper=rsi_upper,
        ignore_rsi=ignore_rsi,
    )
    signal: Signal = "HOLD"

    if market_mode == "DEFENSE":
        reason = "DEFENSE mode: no new BUY; only exit checks"
    elif score >= int(buy_threshold):
        signal = "BUY"
        reason = "BUY signal: " + (" & ".join(parts) if parts else "score matched")
    else:
        # V5 technical sell confirmation for held positions.
        if ind.ma20 is not None and ind.ma60 is not None and ind.ma20 < ind.ma60:
            signal = "SELL"
            reason = "SELL signal: MA20<MA60"
        else:
            reason = "HOLD: no BUY/SELL signal"

    return AnalysisResult(
        code=code,
        score=int(score),
        signal=signal,
        indicators=ind,
        reason=reason,
        market_mode=market_mode,
    )

