from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from agents.debate import run_bull_bear_review
from agents.guardrails import llm_call_allowed, record_llm_call
from agents.memory import append_decision_memory, get_recent_memory_context
from agents.portfolio import action_to_review_decision, run_portfolio_manager
from agents.reports import build_agent_reports
from market_context import classify_news_impact, extract_impact_titles, fetch_news_summary, fetch_order_book_summary


AGENT_RESEARCH_JSONL_PATH = os.path.join("logs", "agent_research_decisions.jsonl")
AGENT_RESEARCH_MD_PATH = os.path.join("logs", "agent_research_decisions.md")
AGENT_RESEARCH_STATE_PATH = os.path.join("logs", "agent_research_state.json")

_REVIEW_DECISIONS = {
    "APPROVE_BUY_REVIEW",
    "CAUTION_BUY_REVIEW",
    "DOWNGRADE_TO_HOLD_REVIEW",
    "APPROVE_SELL_REVIEW",
    "WATCHLIST_REVIEW",
    "WATCH",
}

_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": sorted(_REVIEW_DECISIONS)},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "technical_report": {"type": "string"},
        "fundamental_report": {"type": "string"},
        "bull_case": {"type": "array", "items": {"type": "string"}},
        "bear_case": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "portfolio_review": {"type": "string"},
    },
    "required": [
        "decision",
        "confidence",
        "technical_report",
        "fundamental_report",
        "bull_case",
        "bear_case",
        "risk_flags",
        "portfolio_review",
    ],
}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        v = float(value)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _pct(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{float(value):.1f}%"


def _money(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{float(value):.2f}"


def _load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _str_list(value: Any, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text[:240])
        if len(out) >= int(limit):
            break
    return out


def _normalize_review(raw: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(fallback)
    out = dict(fallback)
    decision = str(raw.get("decision", out.get("decision", "WATCH")) or "WATCH").strip().upper()
    out["decision"] = decision if decision in _REVIEW_DECISIONS else out.get("decision", "WATCH")
    out["confidence"] = max(0, min(100, _safe_int(raw.get("confidence")) or int(out.get("confidence", 0) or 0)))
    for key in ("technical_report", "fundamental_report", "portfolio_review"):
        text = str(raw.get(key, out.get(key, "")) or "").strip()
        out[key] = text[:800] if text else str(out.get(key, ""))
    for key in ("bull_case", "bear_case", "risk_flags"):
        values = _str_list(raw.get(key), limit=8)
        out[key] = values if values else list(out.get(key, []))
    return out


def _extract_response_text(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            ctype = str(content.get("type", ""))
            if ctype in {"output_text", "text"} and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts).strip()


def _call_openai_structured_review(
    ctx: dict[str, Any],
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout_s: float,
    max_retries: int = 1,
) -> dict[str, Any]:
    url = f"{str(base_url).rstrip('/')}/responses"
    instructions = (
        "You are TradePilot's advisory portfolio review layer. Review only the supplied "
        "DecisionContext JSON. Do not invent prices, news, fundamentals, orders, or account data. "
        "You are not allowed to place orders or override the deterministic strategy. Produce a concise "
        "risk review for human inspection using the required schema."
    )
    user_text = (
        "Review this candidate. Prefer conservative wording when evidence conflicts. "
        "Use risk_flags for concrete blockers such as overbought_rsi, short_term_extension, "
        "fundamental_penalty, long_trend_weak, defense_market, buy_pause, account_drawdown, "
        "capital_preservation, insufficient_cash, or concentration_risk.\n\n"
        + json.dumps(ctx, ensure_ascii=False, sort_keys=True)
    )
    payload = {
        "model": model,
        "instructions": instructions,
        "input": user_text,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "tradepilot_agent_research_review",
                "strict": True,
                "schema": _REVIEW_SCHEMA,
            }
        },
        "max_output_tokens": 900,
    }
    last_error: Exception | None = None
    attempts = max(1, int(max_retries) + 1)
    for _ in range(attempts):
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=float(timeout_s),
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"openai_http_{resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            text = _extract_response_text(data)
            if not text:
                raise RuntimeError("openai_empty_output")
            return json.loads(text)
        except Exception as e:
            last_error = e
    raise RuntimeError(str(last_error or "openai_unknown_error"))


def _today_local(tz_name: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _indicator_snapshot(ind: Any) -> dict[str, float | None]:
    return {
        "close": _safe_float(getattr(ind, "close", None)),
        "ma20": _safe_float(getattr(ind, "ma20", None)),
        "ma60": _safe_float(getattr(ind, "ma60", None)),
        "ma100": _safe_float(getattr(ind, "ma100", None)),
        "ma200": _safe_float(getattr(ind, "ma200", None)),
        "rsi14": _safe_float(getattr(ind, "rsi14", None)),
        "ret5d_pct": _safe_float(getattr(ind, "ret5d_pct", None)),
        "ret63_pct": _safe_float(getattr(ind, "ret63_pct", None)),
    }


def _fundamental_snapshot(fscore: Any) -> dict[str, Any]:
    if fscore is None:
        return {
            "available": False,
            "adjustment": 0,
            "size_multiplier": 1.0,
            "reason": "fundamental score unavailable",
        }
    return {
        "available": True,
        "adjustment": _safe_int(getattr(fscore, "adjustment", 0)) or 0,
        "size_multiplier": _safe_float(getattr(fscore, "size_multiplier", 1.0)) or 1.0,
        "age_days": _safe_int(getattr(fscore, "age_days", None)),
        "pe_ttm": _safe_float(getattr(fscore, "pe_ttm", None)),
        "pcf_ttm": _safe_float(getattr(fscore, "pcf_ttm", None)),
        "pe_score": _safe_int(getattr(fscore, "pe_score", None)),
        "cashflow_score": _safe_int(getattr(fscore, "cashflow_score", None)),
        "profit_guard_score": _safe_int(getattr(fscore, "profit_guard_score", None)),
        "reason": str(getattr(fscore, "reason", "") or ""),
    }


def build_decision_context(
    candidate: dict[str, Any],
    *,
    market_mode: str,
    market_allows_buy: bool,
    buy_paused: bool,
    risk_pause_reason: str,
    account_drawdown_pct: float,
    risk_overlay_tone: str,
    risk_overlay_message: str,
    available_cash: float,
    total_assets: float,
    core_symbol: str,
    active_alpha_holding_count: int,
    holding: Any = None,
    news: Any = None,
    order_book: Any = None,
    memory_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ar = candidate.get("ar")
    ind = getattr(ar, "indicators", None)
    current_price = _safe_float(candidate.get("current"))
    qty = int(getattr(holding, "qty", 0) or 0) if holding is not None else 0
    buy_price = _safe_float(getattr(holding, "buy_price", None)) if holding is not None else None
    pnl_pct = None
    if qty > 0 and buy_price and current_price:
        pnl_pct = (float(current_price) / float(buy_price) - 1.0) * 100.0
    news_payload = _news_snapshot(news)
    return {
        "code": str(candidate.get("code", "")).strip().upper(),
        "current_price": current_price,
        "strategy": {
            "signal": str(getattr(ar, "signal", "HOLD")),
            "score": _safe_int(getattr(ar, "score", 0)) or 0,
            "reason": str(getattr(ar, "reason", "") or ""),
            "rank_pct": _safe_float(candidate.get("rank_pct")) or 0.0,
            "relaxed_entry": bool(candidate.get("relaxed_entry", False)),
        },
        "indicators": _indicator_snapshot(ind),
        "fundamental": _fundamental_snapshot(candidate.get("fundamental")),
        "news": news_payload,
        "order_book": _order_book_snapshot(order_book),
        "position": {
            "has_position": qty > 0,
            "qty": qty,
            "buy_price": buy_price,
            "unrealized_pnl_pct": pnl_pct,
            "highest_price_since_entry": _safe_float(getattr(holding, "highest_price_since_entry", None)) if holding is not None else None,
            "trailing_active": bool(getattr(holding, "trailing_active", False)) if holding is not None else False,
            "add_position_count": int(getattr(holding, "add_position_count", 0) or 0) if holding is not None else 0,
            "last_sell_time": str(getattr(holding, "last_sell_time", "") or "") if holding is not None else "",
        },
        "portfolio": {
            "market_mode": str(market_mode),
            "market_allows_buy": bool(market_allows_buy),
            "buy_paused": bool(buy_paused),
            "risk_pause_reason": str(risk_pause_reason or ""),
            "account_drawdown_pct": _safe_float(account_drawdown_pct) or 0.0,
            "risk_overlay_tone": str(risk_overlay_tone or ""),
            "risk_overlay_message": str(risk_overlay_message or ""),
            "available_cash": _safe_float(available_cash) or 0.0,
            "total_assets": _safe_float(total_assets) or 0.0,
            "core_symbol": str(core_symbol).strip().upper(),
            "active_alpha_holding_count": int(active_alpha_holding_count),
        },
        "memory_context": list(memory_context or []),
    }


def _news_snapshot(news: Any) -> dict[str, Any]:
    if news is None:
        return {
            "available": False,
            "verdict": "unavailable",
            "titles": [],
            "impact_titles": [],
            "bullish_hits": [],
            "bearish_hits": [],
        }
    try:
        verdict, bull, bear = classify_news_impact(news)
        return {
            "available": True,
            "verdict": verdict,
            "titles": list(getattr(news, "titles", []) or [])[:8],
            "impact_titles": extract_impact_titles(news)[:5],
            "bullish_hits": list(bull or []),
            "bearish_hits": list(bear or []),
        }
    except Exception:
        return {
            "available": False,
            "verdict": "error",
            "titles": [],
            "impact_titles": [],
            "bullish_hits": [],
            "bearish_hits": [],
        }


def _order_book_snapshot(order_book: Any) -> dict[str, Any]:
    if order_book is None:
        return {"available": False}
    return {
        "available": True,
        "bid_total": _safe_float(getattr(order_book, "bid_total", None)),
        "ask_total": _safe_float(getattr(order_book, "ask_total", None)),
        "imbalance": _safe_float(getattr(order_book, "imbalance", None)),
        "spread": _safe_float(getattr(order_book, "spread", None)),
        "top_bids": list(getattr(order_book, "top_bids", []) or [])[:3],
        "top_asks": list(getattr(order_book, "top_asks", []) or [])[:3],
    }


def _find_holding_any(holdings: list[Any], code: str) -> Any:
    code_u = str(code).strip().upper()
    for h in holdings:
        if str(getattr(h, "symbol", "")).strip().upper() == code_u:
            return h
    return None


def review_decision_context(ctx: dict[str, Any]) -> dict[str, Any]:
    ctx = ctx or {}
    strategy = ctx.get("strategy") or {}
    ind = ctx.get("indicators") or {}
    fundamental = ctx.get("fundamental") or {}
    portfolio = ctx.get("portfolio") or {}

    signal = str(strategy.get("signal", "HOLD"))
    score = int(strategy.get("score", 0) or 0)
    rank_pct = float(strategy.get("rank_pct", 0.0) or 0.0)
    rsi14 = _safe_float(ind.get("rsi14"))
    ret5d = _safe_float(ind.get("ret5d_pct"))
    ret63 = _safe_float(ind.get("ret63_pct"))
    close = _safe_float(ind.get("close"))
    ma20 = _safe_float(ind.get("ma20"))
    ma60 = _safe_float(ind.get("ma60"))
    ma200 = _safe_float(ind.get("ma200"))
    f_adj = int(fundamental.get("adjustment", 0) or 0)
    drawdown = float(portfolio.get("account_drawdown_pct", 0.0) or 0.0)

    bull: list[str] = []
    bear: list[str] = []
    risk_flags: list[str] = []

    if score >= 80:
        bull.append(f"high strategy score ({score})")
    elif score >= 60:
        bull.append(f"strategy score clears buy threshold ({score})")
    else:
        bear.append(f"strategy score below normal buy threshold ({score})")

    if close is not None and ma200 is not None and close > ma200:
        bull.append("price is above MA200")
    elif close is not None and ma200 is not None:
        bear.append("price is below MA200")
        risk_flags.append("long_trend_weak")

    if ma20 is not None and ma60 is not None and ma20 > ma60:
        bull.append("MA20 is above MA60")
    elif ma20 is not None and ma60 is not None:
        bear.append("MA20 is below MA60")
        risk_flags.append("medium_trend_weak")

    if rank_pct >= 0.70:
        bull.append(f"relative strength rank is high ({rank_pct * 100.0:.1f}%)")
    elif rank_pct < 0.35:
        bear.append(f"relative strength rank is weak ({rank_pct * 100.0:.1f}%)")

    if rsi14 is not None and rsi14 > 76:
        bear.append(f"RSI looks extended ({rsi14:.1f})")
        risk_flags.append("overbought_rsi")
    elif rsi14 is not None and 45 <= rsi14 <= 70:
        bull.append(f"RSI is in a workable range ({rsi14:.1f})")

    if ret5d is not None and ret5d > 8:
        bear.append(f"5-day move is stretched ({ret5d:.1f}%)")
        risk_flags.append("short_term_extension")
    elif ret5d is not None and -6 <= ret5d <= 4:
        bull.append(f"5-day move is not overextended ({ret5d:.1f}%)")

    if ret63 is not None and ret63 < -10:
        bear.append(f"63-day return is deeply negative ({ret63:.1f}%)")

    if f_adj > 0:
        bull.append(f"fundamentals add +{f_adj} points")
    elif f_adj < 0:
        bear.append(f"fundamentals subtract {f_adj} points")
        risk_flags.append("fundamental_penalty")

    if str(portfolio.get("market_mode", "")).upper() == "DEFENSE":
        bear.append("market regime is DEFENSE")
        risk_flags.append("defense_market")
    if bool(portfolio.get("buy_paused", False)):
        bear.append("new buys are paused by risk state")
        risk_flags.append("buy_pause")
    if drawdown >= 10:
        bear.append(f"account drawdown is elevated ({drawdown:.1f}%)")
        risk_flags.append("account_drawdown")
    if str(portfolio.get("risk_overlay_tone", "")).upper() == "CAPITAL_PRESERVATION":
        bear.append("risk overlay is in capital preservation mode")
        risk_flags.append("capital_preservation")

    decision = "WATCH"
    if signal == "BUY":
        decision = "APPROVE_BUY_REVIEW"
        if risk_flags:
            decision = "CAUTION_BUY_REVIEW"
        if {"defense_market", "buy_pause", "capital_preservation"} & set(risk_flags):
            decision = "DOWNGRADE_TO_HOLD_REVIEW"
    elif signal == "SELL":
        decision = "APPROVE_SELL_REVIEW"
    elif score >= 55 and not risk_flags:
        decision = "WATCHLIST_REVIEW"

    confidence = max(0, min(100, score + min(10, len(bull) * 2) - min(20, len(risk_flags) * 5)))
    return {
        "decision": decision,
        "confidence": confidence,
        "technical_report": (
            f"Signal={signal}, score={score}, rank={rank_pct * 100.0:.1f}%, "
            f"RSI={_money(rsi14)}, ret5d={_pct(ret5d)}, ret63={_pct(ret63)}."
        ),
        "fundamental_report": str(fundamental.get("reason", "")) or "No fundamental detail available.",
        "bull_case": bull[:8],
        "bear_case": bear[:8],
        "risk_flags": sorted(set(risk_flags)),
        "portfolio_review": (
            f"Mode={portfolio.get('market_mode')}, overlay={portfolio.get('risk_overlay_tone')}, "
            f"cash={_money(_safe_float(portfolio.get('available_cash')))}, "
            f"assets={_money(_safe_float(portfolio.get('total_assets')))}."
        ),
    }


def build_structured_review(ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    reports = build_agent_reports(ctx)
    debate = run_bull_bear_review(ctx, reports)
    portfolio = run_portfolio_manager(ctx, debate)
    fallback = review_decision_context(ctx)
    merged = dict(fallback)
    merged["decision"] = action_to_review_decision(portfolio.get("final_action", "HOLD"))
    merged["confidence"] = int(portfolio.get("confidence", fallback.get("confidence", 0)) or 0)
    merged["bull_case"] = list(debate.get("bull_case") or fallback.get("bull_case") or [])
    merged["bear_case"] = list(debate.get("bear_case") or fallback.get("bear_case") or [])
    merged["risk_flags"] = sorted(set(list(fallback.get("risk_flags") or []) + list(debate.get("risk_flags") or [])))
    merged["technical_report"] = str((reports.get("technical_report") or {}).get("summary") or fallback.get("technical_report", ""))
    merged["fundamental_report"] = str((reports.get("fundamental_report") or {}).get("summary") or fallback.get("fundamental_report", ""))
    merged["portfolio_review"] = str(portfolio.get("rationale") or fallback.get("portfolio_review", ""))
    return merged, reports, debate, portfolio


def _render_markdown_entry(entry: dict[str, Any]) -> str:
    ctx = entry.get("context") or {}
    review = entry.get("review") or {}
    strategy = ctx.get("strategy") or {}
    portfolio_manager = entry.get("portfolio_manager") or {}
    debate = entry.get("debate") or {}
    memory = ctx.get("memory_context") or []
    source = str(entry.get("review_source", "rule"))
    error = str(entry.get("review_error", "") or "")
    lines = [
        f"## {entry['date_local']} {ctx['code']}",
        "",
        f"- Final review: {review['decision']} (confidence {review['confidence']})",
        f"- Portfolio action: {portfolio_manager.get('final_action', 'NA')}",
        f"- Confidence delta: {debate.get('confidence_delta', 0)}",
        f"- Review source: {source}{' fallback=' + error if error else ''}",
        f"- Strategy: {strategy['signal']} score={strategy['score']} rank={strategy['rank_pct'] * 100.0:.1f}%",
        f"- Technical: {review['technical_report']}",
        f"- Portfolio: {review['portfolio_review']}",
        f"- Bull case: {'; '.join(review['bull_case']) if review['bull_case'] else 'none'}",
        f"- Bear case: {'; '.join(review['bear_case']) if review['bear_case'] else 'none'}",
        f"- Risk flags: {', '.join(review['risk_flags']) if review['risk_flags'] else 'none'}",
        f"- Memory: {len(memory)} recent outcome groups",
        f"- Fundamental: {review['fundamental_report']}",
        f"- Original reason: {strategy['reason']}",
        "",
    ]
    return "\n".join(lines)


def load_today_agent_portfolio_actions(*, display_timezone: str) -> dict[str, dict[str, Any]]:
    date_local = _today_local(display_timezone)
    out: dict[str, dict[str, Any]] = {}
    if not os.path.exists(AGENT_RESEARCH_JSONL_PATH):
        return out
    try:
        with open(AGENT_RESEARCH_JSONL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if str(entry.get("date_local", "")) != date_local:
                    continue
                ctx = entry.get("context") or {}
                code = str(ctx.get("code", "")).strip().upper()
                if not code:
                    continue
                pm = entry.get("portfolio_manager") or {}
                review = entry.get("review") or {}
                final_action = str(pm.get("final_action", "") or "")
                if not final_action:
                    decision = str(review.get("decision", "") or "")
                    if decision == "APPROVE_BUY_REVIEW":
                        final_action = "APPROVE_BUY"
                    elif decision == "DOWNGRADE_TO_HOLD_REVIEW":
                        final_action = "DOWNGRADE_TO_HOLD"
                    elif decision == "APPROVE_SELL_REVIEW":
                        final_action = "APPROVE_SELL"
                    elif decision in {"WATCHLIST_REVIEW", "WATCH"}:
                        final_action = "HOLD"
                out[code] = {
                    "final_action": final_action,
                    "portfolio_confidence": _safe_int(pm.get("confidence")) or 0,
                    "review_decision": str(review.get("decision", "") or ""),
                    "review_confidence": _safe_int(review.get("confidence")) or 0,
                    "risk_flags": list(review.get("risk_flags") or []),
                    "review_source": str(entry.get("review_source", "") or ""),
                    "review_error": str(entry.get("review_error", "") or ""),
                }
    except Exception:
        return out
    return out


def run_daily_agent_research(
    candidates: list[dict[str, Any]],
    *,
    top_n: int,
    display_timezone: str,
    market_mode: str,
    market_allows_buy: bool,
    buy_paused: bool,
    risk_pause_reason: str,
    account_drawdown_pct: float,
    risk_overlay_tone: str,
    risk_overlay_message: str,
    available_cash: float,
    total_assets: float,
    core_symbol: str,
    active_alpha_holding_count: int,
    holdings: list[Any] | None = None,
    quote_ctx: Any = None,
    llm_enabled: bool = False,
    llm_api_key: str = "",
    llm_model: str = "gpt-5.1-codex",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_timeout_s: float = 25.0,
    llm_max_retries: int = 1,
    llm_max_calls_per_day: int = 5,
) -> int:
    if not candidates or int(top_n) <= 0:
        return 0

    date_local = _today_local(display_timezone)
    state = _load_json(AGENT_RESEARCH_STATE_PATH, {})
    if state.get("date_local") != date_local:
        state = {"date_local": date_local, "codes": []}
    done = {str(c).upper() for c in state.get("codes", [])}

    ordered = sorted(
        candidates,
        key=lambda x: (-int(getattr(x.get("ar"), "score", 0) or 0), -float(x.get("rank_pct", 0.0) or 0.0), str(x.get("code", ""))),
    )

    entries: list[dict[str, Any]] = []
    for candidate in ordered[: int(top_n)]:
        code = str(candidate.get("code", "")).strip().upper()
        if not code or code in done:
            continue
        holding = _find_holding_any(holdings or [], code)
        news = None
        try:
            news = fetch_news_summary(code, max_items=8, timeout_s=2.5)
        except Exception:
            news = None
        order_book = None
        if quote_ctx is not None:
            try:
                order_book = fetch_order_book_summary(quote_ctx, code, levels=5)
            except Exception:
                order_book = None
        memory_context = get_recent_memory_context(code, limit=3)
        ctx = build_decision_context(
            candidate,
            market_mode=market_mode,
            market_allows_buy=market_allows_buy,
            buy_paused=buy_paused,
            risk_pause_reason=risk_pause_reason,
            account_drawdown_pct=account_drawdown_pct,
            risk_overlay_tone=risk_overlay_tone,
            risk_overlay_message=risk_overlay_message,
            available_cash=available_cash,
            total_assets=total_assets,
            core_symbol=core_symbol,
            active_alpha_holding_count=active_alpha_holding_count,
            holding=holding,
            news=news,
            order_book=order_book,
            memory_context=memory_context,
        )
        fallback_review, reports, debate, portfolio_manager = build_structured_review(ctx)
        review = fallback_review
        review_source = "rule"
        review_error = ""
        if bool(llm_enabled) and str(llm_api_key or "").strip():
            allowed, budget_reason = llm_call_allowed(max_calls_per_day=int(llm_max_calls_per_day))
            if not allowed:
                review_error = budget_reason
            else:
                try:
                    raw_review = _call_openai_structured_review(
                        {
                            "decision_context": ctx,
                            "agent_reports": reports,
                            "bull_bear_review": debate,
                            "portfolio_manager": portfolio_manager,
                        },
                        api_key=str(llm_api_key).strip(),
                        model=str(llm_model or "gpt-5.1-codex").strip(),
                        base_url=str(llm_base_url or "https://api.openai.com/v1").strip(),
                        timeout_s=float(llm_timeout_s),
                        max_retries=int(llm_max_retries),
                    )
                    record_llm_call()
                    review = _normalize_review(raw_review, fallback_review)
                    review_source = "llm"
                except Exception as e:
                    review = fallback_review
                    review_source = "rule"
                    review_error = f"{type(e).__name__}: {str(e)[:240]}"
        elif bool(llm_enabled):
            review_error = "missing_openai_api_key"

        entries.append(
            {
                "date_local": date_local,
                "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "context": ctx,
                "review": review,
                "agent_reports": reports,
                "debate": debate,
                "portfolio_manager": portfolio_manager,
                "review_source": review_source,
                "review_error": review_error,
            }
        )
        append_decision_memory(entries[-1])
        done.add(code)

    if not entries:
        return 0

    os.makedirs("logs", exist_ok=True)
    with open(AGENT_RESEARCH_JSONL_PATH, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    with open(AGENT_RESEARCH_MD_PATH, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(_render_markdown_entry(entry))

    state["codes"] = sorted(done)
    _save_json(AGENT_RESEARCH_STATE_PATH, state)
    return len(entries)
