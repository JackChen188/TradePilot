from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from data_provider import fetch_daily_kline
from indicators import add_indicators, latest_indicators
from strategy import analyze, resolve_market_mode


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    annualized_return_pct: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    sharpe_ratio: float
    benchmark_return_pct: float
    trade_count: int


def _max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100.0 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return float(mdd)


def _annualized_return(total_return_pct: float, periods: int) -> float:
    if periods <= 0:
        return 0.0
    years = periods / 252.0
    if years <= 0:
        return 0.0
    total_mult = 1.0 + total_return_pct / 100.0
    if total_mult <= 0:
        return -100.0
    return float((total_mult ** (1.0 / years) - 1.0) * 100.0)


def _sharpe_ratio(equity_curve: list[float]) -> float:
    if len(equity_curve) < 3:
        return 0.0
    rets: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        cur = equity_curve[i]
        if prev > 0:
            rets.append((cur / prev) - 1.0)
    if len(rets) < 2:
        return 0.0
    s = pd.Series(rets)
    std = float(s.std(ddof=1))
    if std <= 0:
        return 0.0
    return float((s.mean() / std) * (252.0**0.5))


def backtest_symbol(
    quote_ctx,
    symbol: str,
    *,
    initial_cash: float = 10_000.0,
    buy_threshold: int = 75,
    benchmark_symbol: str = "US.VOO",
) -> BacktestResult:
    df = add_indicators(fetch_daily_kline(quote_ctx, symbol, days=1900))
    bm_df = add_indicators(fetch_daily_kline(quote_ctx, benchmark_symbol, days=1900))
    if df.empty:
        return BacktestResult(symbol, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)

    cash = float(initial_cash)
    qty = 0
    entry_price = 0.0
    highest_price = 0.0
    trailing_armed = False
    wins = 0
    closed = 0
    gross_profit = 0.0
    gross_loss = 0.0
    equity_curve: list[float] = []

    regime_by_date: dict[str, str] = {}
    for i in range(220, len(bm_df)):
        row = bm_df.iloc[i]
        dt_key = str(row.get("time_key", i))[:10]
        regime_by_date[dt_key] = resolve_market_mode(latest_indicators(bm_df.iloc[: i + 1]))

    for i in range(220, len(df)):
        row = df.iloc[i]
        window = df.iloc[: i + 1]
        ind = latest_indicators(window)
        px = float(ind.close)
        dt_key = str(row.get("time_key", i))[:10]
        market_mode = regime_by_date.get(dt_key, "ATTACK")
        rank_top = bool(ind.ret63_pct is not None and ind.ret63_pct > 0)
        ar = analyze(
            symbol,
            ind,
            buy_threshold=buy_threshold,
            market_ok=(market_mode == "ATTACK"),
            rank_top=rank_top,
            market_mode=market_mode,
        )

        if qty <= 0 and ar.signal == "BUY" and market_mode == "ATTACK":
            buy_qty = int((cash * 0.2) // px)
            if buy_qty >= 1:
                qty = buy_qty
                cash -= qty * px
                entry_price = px
                highest_price = px
                trailing_armed = False
        elif qty > 0:
            highest_price = max(highest_price, px)
            pnl_pct = (px - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            if pnl_pct >= 12.0:
                trailing_armed = True
            trail_hit = trailing_armed and highest_price > 0 and px <= highest_price * 0.94
            ma60_break = ind.ma60 is not None and px < ind.ma60
            stop_loss = pnl_pct <= -7.0
            if stop_loss or trail_hit or ma60_break:
                cash += qty * px
                pnl = (px - entry_price) * qty
                if pnl > 0:
                    wins += 1
                    gross_profit += pnl
                else:
                    gross_loss += abs(pnl)
                closed += 1
                qty = 0
                entry_price = 0.0
                highest_price = 0.0
                trailing_armed = False

        equity = cash + qty * px
        equity_curve.append(equity)

    final_equity = cash + (qty * float(df.iloc[-1]["close"]))
    total_return_pct = (final_equity / initial_cash - 1.0) * 100.0
    annualized_return_pct = _annualized_return(total_return_pct, len(equity_curve))
    win_rate_pct = (wins / closed * 100.0) if closed > 0 else 0.0
    max_drawdown_pct = _max_drawdown(equity_curve)
    sharpe_ratio = _sharpe_ratio(equity_curve)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    benchmark_return_pct = 0.0
    if not bm_df.empty:
        p0 = float(bm_df.iloc[0]["close"])
        p1 = float(bm_df.iloc[-1]["close"])
        if p0 > 0:
            benchmark_return_pct = (p1 / p0 - 1.0) * 100.0

    return BacktestResult(
        symbol=symbol,
        annualized_return_pct=float(annualized_return_pct),
        total_return_pct=float(total_return_pct),
        max_drawdown_pct=float(max_drawdown_pct),
        win_rate_pct=float(win_rate_pct),
        profit_factor=float(profit_factor),
        sharpe_ratio=float(sharpe_ratio),
        benchmark_return_pct=float(benchmark_return_pct),
        trade_count=int(closed),
    )


def backtest_universe(
    quote_ctx,
    symbols: list[str],
    *,
    initial_cash_per_symbol: float = 10_000.0,
    buy_threshold: int = 75,
    benchmark_symbol: str = "US.VOO",
) -> pd.DataFrame:
    rows: list[dict] = []
    for s in symbols:
        try:
            r = backtest_symbol(
                quote_ctx,
                s,
                initial_cash=initial_cash_per_symbol,
                buy_threshold=buy_threshold,
                benchmark_symbol=benchmark_symbol,
            )
            rows.append(
                {
                    "symbol": r.symbol,
                    "annualized_return_pct": r.annualized_return_pct,
                    "total_return_pct": r.total_return_pct,
                    "max_drawdown_pct": r.max_drawdown_pct,
                    "win_rate_pct": r.win_rate_pct,
                    "profit_factor": r.profit_factor,
                    "sharpe_ratio": r.sharpe_ratio,
                    "benchmark_return_pct": r.benchmark_return_pct,
                    "trade_count": r.trade_count,
                }
            )
        except Exception as e:
            rows.append(
                {
                    "symbol": s,
                    "annualized_return_pct": 0.0,
                    "total_return_pct": 0.0,
                    "max_drawdown_pct": 0.0,
                    "win_rate_pct": 0.0,
                    "profit_factor": 0.0,
                    "sharpe_ratio": 0.0,
                    "benchmark_return_pct": 0.0,
                    "trade_count": 0,
                    "error": f"{type(e).__name__}: {e}",
                }
            )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    valid = out[out["error"].isna()] if "error" in out.columns else out
    if not valid.empty:
        out = pd.concat(
            [
                out,
                pd.DataFrame(
                    [
                        {
                            "symbol": "UNIVERSE_AVG",
                            "annualized_return_pct": float(valid["annualized_return_pct"].mean()),
                            "total_return_pct": float(valid["total_return_pct"].mean()),
                            "max_drawdown_pct": float(valid["max_drawdown_pct"].mean()),
                            "win_rate_pct": float(valid["win_rate_pct"].mean()),
                            "profit_factor": float(valid["profit_factor"].mean()),
                            "sharpe_ratio": float(valid["sharpe_ratio"].mean()),
                            "benchmark_return_pct": float(valid["benchmark_return_pct"].mean()),
                            "trade_count": int(valid["trade_count"].sum()),
                            "error": "",
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
    return out

