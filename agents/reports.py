from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def build_technical_report(ctx: dict[str, Any]) -> dict[str, Any]:
    ind = ctx.get("indicators") or {}
    strategy = ctx.get("strategy") or {}
    close = _num(ind.get("close"))
    ma20 = _num(ind.get("ma20"))
    ma60 = _num(ind.get("ma60"))
    ma200 = _num(ind.get("ma200"))
    rsi = ind.get("rsi14")
    trend = "bullish" if close > ma200 and ma20 > ma60 else "mixed"
    if ma20 < ma60 or close < ma200:
        trend = "bearish"
    return {
        "trend": trend,
        "signal": str(strategy.get("signal", "HOLD")),
        "score": int(strategy.get("score", 0) or 0),
        "rank_pct": float(strategy.get("rank_pct", 0.0) or 0.0),
        "summary": (
            f"signal={strategy.get('signal')} score={strategy.get('score')} "
            f"rank={float(strategy.get('rank_pct', 0.0) or 0.0) * 100.0:.1f}% "
            f"close={close:.2f} ma20={ma20:.2f} ma60={ma60:.2f} ma200={ma200:.2f} rsi={rsi}"
        ),
        "evidence": [
            f"price_vs_ma200={'above' if close > ma200 else 'below_or_unknown'}",
            f"ma20_vs_ma60={'above' if ma20 > ma60 else 'below_or_unknown'}",
            f"ret5d_pct={ind.get('ret5d_pct')}",
            f"ret63_pct={ind.get('ret63_pct')}",
        ],
    }


def build_news_report(ctx: dict[str, Any]) -> dict[str, Any]:
    news = ctx.get("news") or {}
    verdict = str(news.get("verdict", "unavailable"))
    titles = list(news.get("impact_titles") or news.get("titles") or [])[:5]
    return {
        "verdict": verdict,
        "bullish_hits": list(news.get("bullish_hits") or []),
        "bearish_hits": list(news.get("bearish_hits") or []),
        "impact_titles": titles,
        "summary": (
            f"news={verdict}; bull={','.join(news.get('bullish_hits') or []) or 'none'}; "
            f"bear={','.join(news.get('bearish_hits') or []) or 'none'}; titles={len(titles)}"
        ),
    }


def build_fundamental_report(ctx: dict[str, Any]) -> dict[str, Any]:
    f = ctx.get("fundamental") or {}
    adj = int(f.get("adjustment", 0) or 0)
    if adj > 0:
        tone = "supportive"
    elif adj < 0:
        tone = "penalty"
    else:
        tone = "neutral_or_unavailable"
    return {
        "tone": tone,
        "adjustment": adj,
        "size_multiplier": float(f.get("size_multiplier", 1.0) or 1.0),
        "pe_ttm": f.get("pe_ttm"),
        "pcf_ttm": f.get("pcf_ttm"),
        "summary": str(f.get("reason", "")) or "No fundamental detail available.",
    }


def build_position_report(ctx: dict[str, Any]) -> dict[str, Any]:
    p = ctx.get("position") or {}
    portfolio = ctx.get("portfolio") or {}
    qty = int(p.get("qty", 0) or 0)
    pnl_pct = p.get("unrealized_pnl_pct")
    return {
        "has_position": bool(p.get("has_position", False)),
        "qty": qty,
        "unrealized_pnl_pct": pnl_pct,
        "add_position_count": int(p.get("add_position_count", 0) or 0),
        "summary": (
            f"holding={'yes' if qty > 0 else 'no'} qty={qty} pnl_pct={pnl_pct}; "
            f"alpha_holdings={portfolio.get('active_alpha_holding_count')} "
            f"cash={portfolio.get('available_cash')} assets={portfolio.get('total_assets')}"
        ),
    }


def build_agent_reports(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "technical_report": build_technical_report(ctx),
        "news_report": build_news_report(ctx),
        "fundamental_report": build_fundamental_report(ctx),
        "position_report": build_position_report(ctx),
    }
