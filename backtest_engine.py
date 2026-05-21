from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from futu import OpenQuoteContext

from config import FUTU, STRATEGY, TRADE
from data_provider import fetch_daily_kline
from indicators import add_indicators
from strategy import analyze


DEFAULT_POOL = [
    "US.VOO",
    "US.QQQ",
    "US.AAPL",
    "US.MSFT",
    "US.NVDA",
    "US.AMZN",
    "US.META",
    "US.GOOGL",
    "US.HUT",
    "US.MSTR",
    "US.STRF",
    "US.COIN",
    "US.PLTR",
]
WATCHLIST_PATH = os.path.join("config", "watchlist.json")
RECOMMENDED_PATH = os.path.join("config", "recommended_watchlist.json")
RANKING_PATH = os.path.join("logs", "backtest_ranking.csv")
PORTFOLIO_BACKTEST_PATH = os.path.join("logs", "portfolio_backtest.csv")


@dataclass(frozen=True)
class SymbolBacktestResult:
    symbol: str
    total_return: float
    annual_return: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    trade_count: int
    avg_gain: float
    avg_loss: float
    final_equity: float
    backtest_score: int
    skip_reason: str = ""


@dataclass(frozen=True)
class CoreModeResult:
    mode: str
    total_return: float
    annual_return: float
    max_drawdown: float


def _load_watchlist_symbols() -> list[str]:
    if not os.path.exists(WATCHLIST_PATH):
        return DEFAULT_POOL.copy()
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        syms = [str(x).strip().upper() for x in (data.get("symbols") or []) if str(x).strip()]
        return syms if syms else DEFAULT_POOL.copy()
    except Exception:
        return DEFAULT_POOL.copy()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TradePilot V11 backtest engine")
    parser.add_argument("--start", type=str, default="", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="", help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", type=str, default="", help="Comma separated symbols, e.g. US.HUT,US.NVDA")
    return parser.parse_args()


def _resolve_period(start_s: str, end_s: str) -> tuple[date, date]:
    if start_s and end_s:
        return datetime.strptime(start_s, "%Y-%m-%d").date(), datetime.strptime(end_s, "%Y-%m-%d").date()
    end_d = date.today()
    start_d = end_d - timedelta(days=365)
    return start_d, end_d


def _max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    return float(mdd)


def _annual_return(total_return_pct: float, trading_days: int) -> float:
    if trading_days <= 0:
        return 0.0
    years = trading_days / 252.0
    if years <= 0:
        return 0.0
    mult = 1.0 + (total_return_pct / 100.0)
    if mult <= 0:
        return -100.0
    return float((mult ** (1.0 / years) - 1.0) * 100.0)


def _score_backtest(*, annual_return: float, max_drawdown: float, win_rate: float, profit_factor: float, trade_count: int) -> int:
    score = 0
    if annual_return >= 20.0:
        score += 30
    elif annual_return >= 10.0:
        score += 15
    if max_drawdown <= 15.0:
        score += 25
    elif max_drawdown <= 25.0:
        score += 10
    if win_rate >= 50.0:
        score += 15
    if profit_factor >= 1.5:
        score += 20
    if trade_count >= 3:
        score += 10
    return int(score)


def _cash_pct_for_symbol(symbol: str) -> float:
    s = symbol.upper()
    if s in set(TRADE.core_etf_symbols):
        return float(STRATEGY.core_etf_cash_pct)
    if s in set(TRADE.high_vol_symbols):
        return float(STRATEGY.high_vol_cash_pct)
    return float(STRATEGY.quality_tech_cash_pct)


def _prepare_data(quote_ctx, symbols: list[str], start_d: date, end_d: date) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    lookback_days = max(420, int((end_d - start_d).days + 260))
    for symbol in symbols:
        df = add_indicators(fetch_daily_kline(quote_ctx, symbol, days=lookback_days))
        if "time_key" not in df.columns:
            out[symbol] = pd.DataFrame()
            continue
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["time_key"], errors="coerce").dt.date
        df = df[(df["trade_date"] >= start_d) & (df["trade_date"] <= end_d)].reset_index(drop=True)
        out[symbol] = df
    return out


def _build_rank_map(data_map: dict[str, pd.DataFrame]) -> dict[tuple[date, str], float]:
    daily_returns: dict[date, list[tuple[str, float]]] = {}
    for symbol, df in data_map.items():
        if df.empty:
            continue
        for _, row in df.iterrows():
            d = row.get("trade_date")
            r = row.get("ret63_pct")
            if d is None or pd.isna(d) or pd.isna(r):
                continue
            daily_returns.setdefault(d, []).append((symbol, float(r)))
    rank_map: dict[tuple[date, str], float] = {}
    for d, vals in daily_returns.items():
        sorted_vals = sorted(vals, key=lambda x: x[1], reverse=True)
        n = len(sorted_vals)
        for idx, (symbol, _) in enumerate(sorted_vals):
            pct = 1.0 if n == 1 else 1.0 - (idx / float(n - 1))
            rank_map[(d, symbol)] = float(pct)
    return rank_map


def _simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    spy_df: pd.DataFrame,
    rank_map: dict[tuple[date, str], float],
) -> SymbolBacktestResult:
    if df.empty:
        return SymbolBacktestResult(symbol, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 10_000.0, 0, "empty data")
    if len(df) < 120:
        return SymbolBacktestResult(symbol, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 10_000.0, 0, "insufficient history")

    spy_map = {}
    if not spy_df.empty:
        for _, row in spy_df.iterrows():
            spy_map[row["trade_date"]] = (row.get("close"), row.get("ma200"))

    cash = 10_000.0
    qty = 0
    entry_price = 0.0
    trailing_active = False
    highest_price = 0.0
    last_sell_idx = -99999
    entry_idx = -1
    initial_qty = 0
    add_count = 0
    equity_curve: list[float] = []
    sell_pnls: list[float] = []

    for i in range(max(60, int(STRATEGY.reentry_breakout_lookback_days) + 1), len(df)):
        row = df.iloc[i]
        d = row["trade_date"]
        close = float(row["close"])
        ma20 = row.get("ma20")
        ma60 = row.get("ma60")
        ma200 = row.get("ma200")
        rsi14 = row.get("rsi14")
        ret5 = row.get("ret5d_pct")
        if d not in spy_map:
            equity_curve.append(cash + qty * close)
            continue
        spy_close, spy_ma200 = spy_map[d]
        market_ok = pd.notna(spy_close) and pd.notna(spy_ma200) and float(spy_close) > float(spy_ma200)
        rank_pct = float(rank_map.get((d, symbol), 0.0))
        rank_top = rank_pct >= float(STRATEGY.rank_top_pct_threshold)
        strong_exception = symbol in set(TRADE.strong_exception_symbols)
        market_mode = "ATTACK" if market_ok else "DEFENSE"

        ind_like = type("Ind", (), {})()
        ind_like.close = close
        ind_like.ma20 = None if pd.isna(ma20) else float(ma20)
        ind_like.ma60 = None if pd.isna(ma60) else float(ma60)
        ind_like.ma100 = None if pd.isna(row.get("ma100")) else float(row.get("ma100"))
        ind_like.ma200 = None if pd.isna(ma200) else float(ma200)
        ind_like.rsi14 = None if pd.isna(rsi14) else float(rsi14)
        ind_like.ret5d_pct = None if pd.isna(ret5) else float(ret5)
        ind_like.ret63_pct = None if pd.isna(row.get("ret63_pct")) else float(row.get("ret63_pct"))

        ar = analyze(
            symbol,
            ind_like,
            buy_threshold=STRATEGY.buy_score_threshold,
            market_ok=market_ok,
            rank_top=rank_top,
            market_mode=market_mode,
            strong_exception=strong_exception,
            rsi_upper=STRATEGY.strong_rsi_upper,
            ignore_rsi=(symbol in set(TRADE.no_rsi_limit_symbols)),
        )
        relaxed_entry = (
            ind_like.ma100 is not None
            and ind_like.ma20 is not None
            and ind_like.ma60 is not None
            and float(close) > float(ind_like.ma100)
            and float(ind_like.ma20) > float(ind_like.ma60)
        )
        if i < int(STRATEGY.v8_chop_window_days):
            choppy = False
        else:
            c0 = float(df.iloc[i - int(STRATEGY.v8_chop_window_days)]["close"])
            choppy = False
            if c0 > 0:
                choppy = abs((close / c0 - 1.0) * 100.0) > float(STRATEGY.v8_chop_abs_ret_pct)

        if qty <= 0:
            breakout_ok = True
            is_high_vol = symbol.upper() in set(TRADE.high_vol_alpha_symbols)
            if last_sell_idx >= 0:
                lookback = int(STRATEGY.reentry_breakout_lookback_days)
                min_wait = (
                    int(STRATEGY.alpha_trend_reentry_wait_days)
                    if symbol.upper() in set(TRADE.trend_core_alpha_symbols)
                    else int(STRATEGY.alpha_high_vol_reentry_wait_days)
                    if is_high_vol
                    else int(STRATEGY.reentry_min_days_since_sell)
                )
                if i - last_sell_idx < min_wait:
                    breakout_ok = False
                else:
                    prev = df.iloc[max(0, i - lookback) : i]
                    if prev.empty:
                        breakout_ok = False
                    else:
                        breakout_ok = close > float(prev["close"].max())
            # HIGH_VOL entry hardening: require breakout even for first entry and higher score threshold.
            if is_high_vol and bool(STRATEGY.alpha_high_vol_require_breakout):
                prev = df.iloc[max(0, i - int(STRATEGY.v8_breakout_lookback_days)) : i]
                if prev.empty:
                    breakout_ok = False
                else:
                    breakout_ok = breakout_ok and (close > float(prev["close"].max()))
            allow_buy = ar.signal == "BUY" or relaxed_entry
            if is_high_vol and int(ar.score) < int(STRATEGY.alpha_high_vol_buy_score_threshold):
                allow_buy = False
            if market_ok and allow_buy and breakout_ok:
                cash_pct = float(STRATEGY.v8_initial_cash_pct)
                equity = cash
                budget = min(cash * cash_pct, equity * 0.30, cash)
                buy_qty = int(budget // close)
                if buy_qty >= 1:
                    qty = int(buy_qty)
                    cash -= qty * close
                    entry_price = close
                    trailing_active = False
                    highest_price = close
                    entry_idx = i
                    initial_qty = qty
                    add_count = 0
        else:
            pnl_pct = (close - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
            hold_days = i - int(entry_idx)
            is_high_vol = symbol.upper() in set(TRADE.high_vol_alpha_symbols)
            stop_loss_pct = float(STRATEGY.alpha_high_vol_stop_loss_pct if is_high_vol else STRATEGY.alpha_trend_stop_loss_pct)
            trailing_pct = float(STRATEGY.alpha_high_vol_trailing_pct if is_high_vol else STRATEGY.alpha_trend_trailing_pct)
            allow_add = (ar.signal == "BUY" or relaxed_entry) and (symbol.upper() in set(TRADE.high_vol_alpha_symbols))
            if market_ok and allow_add and add_count < int(STRATEGY.v8_max_add_count):
                lookback = int(STRATEGY.v8_breakout_lookback_days)
                prev = df.iloc[max(0, i - lookback) : i]
                if not prev.empty and close > float(prev["close"].max()):
                    equity = cash + qty * close
                    budget = min(cash * float(STRATEGY.v8_add_cash_pct), equity * 0.30 - qty * close, cash)
                    add_qty = int(max(0.0, budget) // close)
                    if add_qty > 0:
                        if initial_qty > 0:
                            add_qty = min(add_qty, int(initial_qty))
                        if add_qty > 0:
                            qty += int(add_qty)
                            cash -= add_qty * close
                            add_count += 1
                            highest_price = max(highest_price, close)
            if pnl_pct <= -float(stop_loss_pct):
                pnl = (close - entry_price) * qty
                cash += qty * close
                sell_pnls.append(float(pnl))
                qty = 0
                entry_price = 0.0
                trailing_active = False
                highest_price = 0.0
                last_sell_idx = i
                entry_idx = -1
                initial_qty = 0
                add_count = 0
            else:
                trend_break = pd.notna(ma20) and pd.notna(ma60) and float(ma20) < float(ma60)
                if trend_break and hold_days >= int(STRATEGY.min_holding_days):
                    pnl = (close - entry_price) * qty
                    cash += qty * close
                    sell_pnls.append(float(pnl))
                    qty = 0
                    entry_price = 0.0
                    trailing_active = False
                    highest_price = 0.0
                    last_sell_idx = i
                    entry_idx = -1
                    initial_qty = 0
                    add_count = 0
                else:
                    if pnl_pct >= float(STRATEGY.trailing_activate_pct):
                        trailing_active = True
                    if trailing_active:
                        highest_price = max(highest_price, close)
                        if highest_price > 0:
                            dd_from_peak = (highest_price - close) / highest_price * 100.0
                            if dd_from_peak >= float(trailing_pct) and hold_days >= int(STRATEGY.min_holding_days):
                                pnl = (close - entry_price) * qty
                                cash += qty * close
                                sell_pnls.append(float(pnl))
                                qty = 0
                                entry_price = 0.0
                                trailing_active = False
                                highest_price = 0.0
                                last_sell_idx = i
                                entry_idx = -1
                                initial_qty = 0
                                add_count = 0

        equity_curve.append(cash + qty * close)

    final_price = float(df.iloc[-1]["close"])
    final_equity = float(cash + qty * final_price)
    total_return = (final_equity / 10_000.0 - 1.0) * 100.0
    annual_return = _annual_return(total_return, len(equity_curve))
    max_drawdown = _max_drawdown(equity_curve)
    wins = [x for x in sell_pnls if x > 0]
    losses = [x for x in sell_pnls if x < 0]
    trade_count = len(sell_pnls)
    win_rate = (len(wins) / trade_count * 100.0) if trade_count > 0 else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    avg_gain = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    score = _score_backtest(
        annual_return=annual_return,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        profit_factor=profit_factor,
        trade_count=trade_count,
    )
    return SymbolBacktestResult(
        symbol=symbol,
        total_return=float(total_return),
        annual_return=float(annual_return),
        max_drawdown=float(max_drawdown),
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
        trade_count=int(trade_count),
        avg_gain=float(avg_gain),
        avg_loss=float(avg_loss),
        final_equity=float(final_equity),
        backtest_score=int(score),
        skip_reason="",
    )


def _benchmark_buy_hold(voo_df: pd.DataFrame) -> float:
    if voo_df.empty:
        return 0.0
    p0 = float(voo_df.iloc[0]["close"])
    p1 = float(voo_df.iloc[-1]["close"])
    if p0 <= 0:
        return 0.0
    return (p1 / p0 - 1.0) * 100.0


def _simulate_core_buy_hold(voo_df: pd.DataFrame, initial_cash: float = 10_000.0) -> CoreModeResult:
    if voo_df.empty:
        return CoreModeResult("VOO_BH", 0.0, 0.0, 0.0)
    p0 = float(voo_df.iloc[0]["close"])
    if p0 <= 0:
        return CoreModeResult("VOO_BH", 0.0, 0.0, 0.0)
    qty = int(initial_cash // p0)
    cash = initial_cash - qty * p0
    curve: list[float] = []
    for _, row in voo_df.iterrows():
        px = float(row["close"])
        curve.append(cash + qty * px)
    final_eq = curve[-1] if curve else initial_cash
    total = (final_eq / initial_cash - 1.0) * 100.0
    annual = _annual_return(total, len(curve))
    mdd = _max_drawdown(curve)
    return CoreModeResult("VOO_BH", float(total), float(annual), float(mdd))


def _simulate_core_dca(
    voo_df: pd.DataFrame,
    spy_df: pd.DataFrame,
    *,
    use_dip: bool,
    initial_cash: float = 10_000.0,
) -> CoreModeResult:
    if voo_df.empty:
        return CoreModeResult("VOO_DCA_DIP" if use_dip else "VOO_DCA", 0.0, 0.0, 0.0)
    spy_map = {}
    if not spy_df.empty:
        for _, row in spy_df.iterrows():
            spy_map[row["trade_date"]] = (row.get("close"), row.get("ma200"))

    qty = 0
    cash = float(initial_cash)
    chunk = float(initial_cash) / float(max(int(STRATEGY.core_dca_chunks), 1))
    last_buy_idx = -10**9
    dip5_done = False
    dip10_done = False
    recent_high = 0.0
    curve: list[float] = []

    for i, row in voo_df.iterrows():
        d = row["trade_date"]
        px = float(row["close"])
        recent_high = max(recent_high, px)
        market_ok = True
        crash_pause = False
        if d in spy_map:
            sc, sma = spy_map[d]
            market_ok = pd.notna(sc) and pd.notna(sma) and float(sc) > float(sma)
            if "close" in spy_df.columns:
                idx = min(i, len(spy_df) - 1)
                high_52w = float(spy_df.iloc[max(0, idx - 251) : idx + 1]["close"].max())
                if high_52w > 0 and pd.notna(sc):
                    dd = (high_52w - float(sc)) / high_52w * 100.0
                    crash_pause = dd >= float(STRATEGY.spy_drawdown_pause_pct)
        if market_ok and (not crash_pause):
            if i - last_buy_idx >= int(STRATEGY.core_dca_interval_days):
                buy_cash = min(cash, chunk)
                buy_qty = int(buy_cash // px)
                if buy_qty > 0:
                    qty += buy_qty
                    cash -= buy_qty * px
                    last_buy_idx = i
            if use_dip and recent_high > 0:
                pullback = (px / recent_high) - 1.0
                if pullback <= float(STRATEGY.core_dip_level_1) and not dip5_done:
                    buy_cash = min(cash, chunk * float(STRATEGY.core_dip_multiplier_1))
                    buy_qty = int(buy_cash // px)
                    if buy_qty > 0:
                        qty += buy_qty
                        cash -= buy_qty * px
                        dip5_done = True
                if pullback <= float(STRATEGY.core_dip_level_2) and not dip10_done:
                    buy_cash = min(cash, chunk * float(STRATEGY.core_dip_multiplier_2))
                    buy_qty = int(buy_cash // px)
                    if buy_qty > 0:
                        qty += buy_qty
                        cash -= buy_qty * px
                        dip10_done = True
        curve.append(cash + qty * px)

    final_eq = curve[-1] if curve else initial_cash
    total = (final_eq / initial_cash - 1.0) * 100.0
    annual = _annual_return(total, len(curve))
    mdd = _max_drawdown(curve)
    mode = "VOO_DCA_DIP" if use_dip else "VOO_DCA"
    return CoreModeResult(mode, float(total), float(annual), float(mdd))


def _write_ranking(results: list[SymbolBacktestResult]) -> None:
    os.makedirs(os.path.dirname(RANKING_PATH), exist_ok=True)
    fields = [
        "symbol",
        "total_return",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "trade_count",
        "avg_gain",
        "avg_loss",
        "final_equity",
        "backtest_score",
        "skip_reason",
    ]
    with open(RANKING_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow(
                {
                    "symbol": r.symbol,
                    "total_return": f"{r.total_return:.2f}",
                    "annual_return": f"{r.annual_return:.2f}",
                    "max_drawdown": f"{r.max_drawdown:.2f}",
                    "win_rate": f"{r.win_rate:.2f}",
                    "profit_factor": f"{r.profit_factor:.4f}",
                    "trade_count": r.trade_count,
                    "avg_gain": f"{r.avg_gain:.2f}",
                    "avg_loss": f"{r.avg_loss:.2f}",
                    "final_equity": f"{r.final_equity:.2f}",
                    "backtest_score": r.backtest_score,
                    "skip_reason": r.skip_reason,
                }
            )


def _write_recommended(results: list[SymbolBacktestResult]) -> None:
    qualified = [x.symbol for x in results if x.backtest_score >= 60 and not x.skip_reason][:8]
    os.makedirs(os.path.dirname(RECOMMENDED_PATH), exist_ok=True)
    with open(RECOMMENDED_PATH, "w", encoding="utf-8") as f:
        json.dump({"symbols": qualified, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)


def _write_portfolio_backtest(
    *,
    start: date,
    end: date,
    core_mode: str,
    core_return_pct: float,
    core_annual_return_pct: float,
    core_max_drawdown_pct: float,
    alpha_return_pct: float,
    alpha_annual_return_pct: float,
    alpha_max_drawdown_pct: float,
    portfolio_return_pct: float,
    portfolio_annual_return_pct: float,
    portfolio_max_drawdown_pct: float,
    outperform_voo: bool,
) -> None:
    os.makedirs(os.path.dirname(PORTFOLIO_BACKTEST_PATH), exist_ok=True)
    exists = os.path.exists(PORTFOLIO_BACKTEST_PATH) and os.path.getsize(PORTFOLIO_BACKTEST_PATH) > 0
    fields = [
        "start",
        "end",
        "core_mode",
        "core_return_pct",
        "core_annual_return_pct",
        "core_max_drawdown_pct",
        "alpha_return_pct",
        "alpha_annual_return_pct",
        "alpha_max_drawdown_pct",
        "portfolio_return_pct",
        "portfolio_annual_return_pct",
        "portfolio_max_drawdown_pct",
        "outperform_voo",
        "core_ratio",
        "alpha_ratio",
    ]
    with open(PORTFOLIO_BACKTEST_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(
            {
                "start": str(start),
                "end": str(end),
                "core_mode": core_mode,
                "core_return_pct": f"{core_return_pct:.2f}",
                "core_annual_return_pct": f"{core_annual_return_pct:.2f}",
                "core_max_drawdown_pct": f"{core_max_drawdown_pct:.2f}",
                "alpha_return_pct": f"{alpha_return_pct:.2f}",
                "alpha_annual_return_pct": f"{alpha_annual_return_pct:.2f}",
                "alpha_max_drawdown_pct": f"{alpha_max_drawdown_pct:.2f}",
                "portfolio_return_pct": f"{portfolio_return_pct:.2f}",
                "portfolio_annual_return_pct": f"{portfolio_annual_return_pct:.2f}",
                "portfolio_max_drawdown_pct": f"{portfolio_max_drawdown_pct:.2f}",
                "outperform_voo": "YES" if outperform_voo else "NO",
                "core_ratio": f"{float(STRATEGY.core_ratio):.2f}",
                "alpha_ratio": f"{float(STRATEGY.alpha_ratio):.2f}",
            }
        )


def main() -> None:
    args = _parse_args()
    start_d, end_d = _resolve_period(args.start, args.end)
    if str(args.symbols or "").strip():
        symbols = [x.strip().upper() for x in str(args.symbols).split(",") if x.strip()]
    else:
        symbols = _load_watchlist_symbols()
    print(f"Backtest period: {start_d} -> {end_d}")
    print(f"Pool size: {len(symbols)}")

    quote_ctx = OpenQuoteContext(host=FUTU.host, port=FUTU.port)
    try:
        pool_for_data = sorted(set(symbols + [TRADE.regime_symbol, TRADE.benchmark_symbol]))
        data_map = _prepare_data(quote_ctx, pool_for_data, start_d, end_d)
        spy_df = data_map.get(TRADE.regime_symbol, pd.DataFrame())
        if spy_df.empty:
            raise RuntimeError(f"SPY regime data missing for {TRADE.regime_symbol}")
        rank_map = _build_rank_map({k: v for k, v in data_map.items() if k in symbols})

        results: list[SymbolBacktestResult] = []
        for s in symbols:
            df = data_map.get(s, pd.DataFrame())
            if df.empty:
                results.append(
                    SymbolBacktestResult(
                        symbol=s,
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
                        skip_reason="history unavailable",
                    )
                )
                continue
            try:
                results.append(_simulate_symbol(s, df, spy_df, rank_map))
            except Exception as e:
                results.append(
                    SymbolBacktestResult(
                        symbol=s,
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
                        skip_reason=f"{type(e).__name__}: {e}",
                    )
                )

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
            outperform = portfolio_return > float(voo_bh)
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
                outperform_voo=outperform,
            )
        print("\n=== Backtest Ranking (Top 12) ===")
        for r in ranked[:12]:
            print(
                f"{r.symbol:10s} score={r.backtest_score:3d} ann={r.annual_return:7.2f}% "
                f"ret={r.total_return:7.2f}% mdd={r.max_drawdown:6.2f}% pf={r.profit_factor:5.2f} "
                f"trades={r.trade_count:3d} skip={r.skip_reason or '-'}"
            )
        print("\n=== Benchmark Compare ===")
        print(f"VOO buy-and-hold return:    {voo_bh:.2f}%")
        print(f"ALPHA strategy return:      {alpha_return:.2f}%")
        for cm in core_modes:
            p_ret = float(STRATEGY.core_ratio) * float(cm.total_return) + float(STRATEGY.alpha_ratio) * float(alpha_return)
            print(
                f"{cm.mode:11s} CORE={cm.total_return:7.2f}% | COMBO={p_ret:7.2f}% | "
                f"OutperformVOO={'YES' if p_ret > voo_bh else 'NO'}"
            )
        print(f"\nSaved ranking: {RANKING_PATH}")
        print(f"Saved portfolio backtest: {PORTFOLIO_BACKTEST_PATH}")
        print(f"Saved recommended watchlist: {RECOMMENDED_PATH}")
    finally:
        quote_ctx.close()


if __name__ == "__main__":
    main()
