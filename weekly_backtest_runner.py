from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
from futu import OpenQuoteContext

from backtest_engine import (
    SymbolBacktestResult,
    _build_rank_map,
    _load_watchlist_symbols,
    _prepare_data,
    _simulate_core_buy_hold,
    _simulate_core_dca,
    _simulate_symbol,
    _write_portfolio_backtest,
    _write_ranking,
    _write_recommended,
)
from config import FUTU, STRATEGY, TRADE


WEEKLY_BACKTEST_STATE_PATH = os.path.join("logs", "weekly_backtest_state.json")
WEEKLY_BACKTEST_SUMMARY_PATH = os.path.join("logs", "weekly_backtest_summary.json")
STRATEGY_OPTIMIZATION_PATH = os.path.join("logs", "strategy_optimization.json")


def _load_json(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def should_run_weekly_backtest_now(*, target_weekday: int, target_hhmm: str) -> bool:
    now = datetime.now(timezone.utc)
    hh, mm = [int(x) for x in str(target_hhmm).split(":", 1)]
    if now.weekday() != int(target_weekday):
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    state = _load_json(WEEKLY_BACKTEST_STATE_PATH)
    return state.get("last_run_date") != now.strftime("%Y-%m-%d")


def mark_weekly_backtest_run(summary: dict) -> None:
    now = datetime.now(timezone.utc)
    _save_json(
        WEEKLY_BACKTEST_STATE_PATH,
        {
            "last_run_date": now.strftime("%Y-%m-%d"),
            "last_run_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "summary_path": WEEKLY_BACKTEST_SUMMARY_PATH,
            "top_symbols": summary.get("top_symbols", []),
        },
    )


def _default_symbols() -> list[str]:
    symbols = _load_watchlist_symbols()
    try:
        symbols.extend(list(TRADE.alpha_extended_symbols))
        symbols.extend(list(TRADE.trend_core_alpha_symbols))
        symbols.extend(list(TRADE.high_vol_alpha_symbols))
    except Exception:
        pass
    core = str(TRADE.core_symbol).strip().upper()
    return sorted({str(s).strip().upper() for s in symbols if str(s).strip().upper() != core})


def _empty_result(symbol: str, reason: str) -> SymbolBacktestResult:
    return SymbolBacktestResult(
        symbol=symbol,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        trade_count=0,
        avg_gain=0.0,
        avg_loss=0.0,
        final_equity=10_000.0,
        backtest_score=0,
        skip_reason=reason,
    )


def _write_optimization_summary(ranked: list[SymbolBacktestResult], *, start_d: date, end_d: date) -> dict:
    valid = [r for r in ranked if not r.skip_reason]
    top = [r for r in valid if r.backtest_score >= 60][:8]
    weak = [r for r in valid if r.backtest_score < 40][-8:]
    avg_score = sum(r.backtest_score for r in valid) / len(valid) if valid else 0.0
    avg_ann = sum(r.annual_return for r in valid) / len(valid) if valid else 0.0
    suggestions: list[str] = []
    if top:
        suggestions.append("Use top backtested symbols as an additional Alpha universe layer.")
    if avg_score < 45:
        suggestions.append("Backtest quality is weak; keep buy thresholds conservative this week.")
    if avg_ann < 0:
        suggestions.append("Average annual return is negative; prefer smaller Alpha exposure until next review.")
    if not suggestions:
        suggestions.append("Backtest quality is acceptable; keep current scoring parameters.")

    payload = {
        "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": {"start": str(start_d), "end": str(end_d), "years": 3},
        "top_symbols": [r.symbol for r in top],
        "weak_symbols": [r.symbol for r in weak],
        "avg_backtest_score": round(avg_score, 2),
        "avg_annual_return_pct": round(avg_ann, 2),
        "suggestions": suggestions,
    }
    _save_json(STRATEGY_OPTIMIZATION_PATH, payload)
    return payload


def run_weekly_backtest_3y(symbols: list[str] | None = None) -> dict[str, Any]:
    end_d = date.today()
    start_d = end_d - timedelta(days=365 * 3)
    symbols = symbols if symbols is not None else _default_symbols()
    symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})

    quote_ctx = OpenQuoteContext(host=FUTU.host, port=FUTU.port)
    try:
        pool_for_data = sorted(set(symbols + [TRADE.regime_symbol, TRADE.benchmark_symbol, TRADE.core_symbol]))
        data_map = _prepare_data(quote_ctx, pool_for_data, start_d, end_d)
        spy_df = data_map.get(TRADE.regime_symbol, pd.DataFrame())
        if spy_df.empty:
            raise RuntimeError(f"SPY regime data missing for {TRADE.regime_symbol}")
        rank_map = _build_rank_map({k: v for k, v in data_map.items() if k in symbols})

        results: list[SymbolBacktestResult] = []
        for symbol in symbols:
            df = data_map.get(symbol, pd.DataFrame())
            if df.empty:
                results.append(_empty_result(symbol, "history unavailable"))
                continue
            try:
                results.append(_simulate_symbol(symbol, df, spy_df, rank_map))
            except Exception as exc:
                results.append(_empty_result(symbol, f"{type(exc).__name__}: {exc}"))

        ranked = sorted(results, key=lambda x: (-x.backtest_score, -x.annual_return, x.symbol))
        _write_ranking(ranked)
        _write_recommended(ranked)

        valid = [x for x in ranked if not x.skip_reason]
        alpha_return = sum([x.total_return for x in valid]) / max(1, len(valid))
        alpha_annual = sum([x.annual_return for x in valid]) / max(1, len(valid))
        alpha_mdd = sum([x.max_drawdown for x in valid]) / max(1, len(valid))

        voo_df = data_map.get(TRADE.core_symbol, pd.DataFrame())
        core_modes = [
            _simulate_core_buy_hold(voo_df),
            _simulate_core_dca(voo_df, spy_df, use_dip=False),
            _simulate_core_dca(voo_df, spy_df, use_dip=True),
        ]
        voo_bh = core_modes[0].total_return
        for cm in core_modes:
            portfolio_return = float(STRATEGY.core_ratio) * float(cm.total_return) + float(STRATEGY.alpha_ratio) * float(alpha_return)
            portfolio_annual = float(STRATEGY.core_ratio) * float(cm.annual_return) + float(STRATEGY.alpha_ratio) * float(alpha_annual)
            portfolio_mdd = float(STRATEGY.core_ratio) * float(cm.max_drawdown) + float(STRATEGY.alpha_ratio) * float(alpha_mdd)
            _write_portfolio_backtest(
                start=start_d,
                end=end_d,
                core_mode=cm.mode,
                core_return_pct=cm.total_return,
                core_annual_return_pct=cm.annual_return,
                core_max_drawdown_pct=cm.max_drawdown,
                alpha_return_pct=alpha_return,
                alpha_annual_return_pct=alpha_annual,
                alpha_max_drawdown_pct=alpha_mdd,
                portfolio_return_pct=portfolio_return,
                portfolio_annual_return_pct=portfolio_annual,
                portfolio_max_drawdown_pct=portfolio_mdd,
                outperform_voo=portfolio_return > float(voo_bh),
            )

        optimization = _write_optimization_summary(ranked, start_d=start_d, end_d=end_d)
        summary = {
            "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period": {"start": str(start_d), "end": str(end_d), "years": 3},
            "symbol_count": len(symbols),
            "valid_count": len(valid),
            "top_symbols": optimization.get("top_symbols", []),
            "top_results": [asdict(r) for r in ranked[:12]],
            "alpha": {
                "avg_total_return_pct": round(alpha_return, 2),
                "avg_annual_return_pct": round(alpha_annual, 2),
                "avg_max_drawdown_pct": round(alpha_mdd, 2),
            },
            "voo_buy_hold_return_pct": round(float(voo_bh), 2),
            "optimization_path": STRATEGY_OPTIMIZATION_PATH,
        }
        _save_json(WEEKLY_BACKTEST_SUMMARY_PATH, summary)
        return summary
    finally:
        quote_ctx.close()


def format_weekly_backtest_summary(summary: dict) -> str:
    period = summary.get("period", {})
    lines = [
        "每周3年回测完成",
        f"区间: {period.get('start')} -> {period.get('end')}",
        f"股票数: {summary.get('symbol_count')} 有效: {summary.get('valid_count')}",
        f"VOO买入持有: {float(summary.get('voo_buy_hold_return_pct', 0.0)):.2f}%",
    ]
    alpha = summary.get("alpha", {})
    lines.append(
        f"Alpha均值: 总收益 {float(alpha.get('avg_total_return_pct', 0.0)):.2f}% "
        f"年化 {float(alpha.get('avg_annual_return_pct', 0.0)):.2f}% "
        f"回撤 {float(alpha.get('avg_max_drawdown_pct', 0.0)):.2f}%"
    )
    top = summary.get("top_symbols") or []
    lines.append("推荐候选池: " + (", ".join(top) if top else "无"))
    opt = _load_json(STRATEGY_OPTIMIZATION_PATH)
    suggestions = opt.get("suggestions") or []
    if suggestions:
        lines.append("策略建议: " + " | ".join(str(x) for x in suggestions[:3]))
    return "\n".join(lines)
