"""
Research-only long-only backtest (NOT wired into live trading).

- Loads daily data via Futu OpenD (same stack as TradePilot).
- Sleeve: fixed ``voo_weight`` in US.VOO, remainder equal-weight among top ``n_stock`` names
  by ``alpha_multifactor.composite_factor_score`` at each rebalance.
- Prints total return, annualized return, max drawdown, Sharpe/Sortino (daily), vs VOO buy-hold.

Example::

    py -3 multifactor_backtest.py --start 2024-04-29 --end 2026-04-29

Conflict note: live ``main.py`` keeps SPY MA200 regime and manual confirms; this script is for offline analysis.
"""
from __future__ import annotations

import argparse
import math
import os
from datetime import date, datetime

import numpy as np
import pandas as pd
from futu import OpenQuoteContext

from alpha_multifactor import composite_factor_score, load_symbol_list_json, score_from_kline_df
from config import FUTU, TRADE
from data_provider import fetch_daily_kline
from indicators import add_indicators


def _parse_iso(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def _month_starts(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    y, m = d0.year, d0.month
    while True:
        cur = date(y, m, 1)
        if cur > d1:
            break
        if cur >= d0:
            out.append(cur)
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return out


def _max_drawdown_pct(curve: list[float]) -> float:
    peak = curve[0]
    mdd = 0.0
    for x in curve:
        peak = max(peak, x)
        if peak <= 0:
            continue
        dd = (peak - x) / peak * 100.0
        mdd = max(mdd, dd)
    return float(mdd)


def _sharpe_sortino(daily_rets: list[float], *, rf: float = 0.0) -> tuple[float, float]:
    arr = np.array([float(x) - rf / 252.0 for x in daily_rets], dtype=float)
    if len(arr) < 5:
        return 0.0, 0.0
    mu = float(arr.mean())
    sd = float(arr.std(ddof=1))
    sharpe = (mu / sd) * math.sqrt(252.0) if sd > 1e-12 else 0.0
    neg = arr[arr < 0.0]
    ds = float(neg.std(ddof=1)) if len(neg) > 1 else 0.0
    sortino = (mu / ds) * math.sqrt(252.0) if ds > 1e-12 else 0.0
    return float(sharpe), float(sortino)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=str, default="2024-04-29")
    ap.add_argument("--end", type=str, default="2026-04-29")
    ap.add_argument("--voo-weight", type=float, default=0.30)
    ap.add_argument("--n-stock", type=int, default=12)
    ap.add_argument("--initial", type=float, default=100_000.0)
    ap.add_argument("--universe-json", type=str, default=os.path.join("config", "sp500_universe.json"))
    args = ap.parse_args()

    start_d = _parse_iso(args.start)
    end_d = _parse_iso(args.end)
    core = str(TRADE.core_symbol).strip().upper()
    universe = [s for s in load_symbol_list_json(args.universe_json) if s.upper() != core.upper()]
    if not universe:
        raise SystemExit("Universe empty; populate config/sp500_universe.json")

    quote_ctx = OpenQuoteContext(host=FUTU.host, port=FUTU.port)
    try:
        daily_index: list[date] = []
        px: dict[str, pd.Series] = {}

        def load_sym(sym: str) -> pd.DataFrame:
            raw = fetch_daily_kline(quote_ctx, sym, days=max(1200, (end_d - start_d).days + 400))
            df = raw.copy()
            df["trade_date"] = pd.to_datetime(df["time_key"], errors="coerce").dt.date
            df = df.dropna(subset=["trade_date"])
            return df

        voo_df = load_sym(core)
        for sym in universe:
            try:
                dfi = load_sym(sym)
                if dfi.empty:
                    continue
                s = pd.to_numeric(dfi.set_index("trade_date")["close"], errors="coerce")
                px[sym] = s
            except Exception:
                continue

        voo_s = pd.to_numeric(voo_df.set_index("trade_date")["close"], errors="coerce")
        all_days = sorted(set(voo_s.index.tolist()) | set().union(*[set(px[k].index.tolist()) for k in px]))
        all_days = [d for d in all_days if start_d <= d <= end_d]
        if len(all_days) < 50:
            raise SystemExit("Not enough overlapping calendar days in range")

        reb_days = [d for d in _month_starts(start_d, end_d) if d in set(all_days)]
        if not reb_days:
            reb_days = [all_days[0]]

        cash = float(args.initial)
        holdings: dict[str, float] = {}
        equity_curve: list[float] = []
        voo_only_curve: list[float] = []
        voo_qty = float(args.initial) / float(voo_s.loc[all_days[0]])

        def mark_to_market(day: date) -> float:
            eq = cash
            for sym, qty in holdings.items():
                if sym not in px or day not in px[sym].index:
                    continue
                eq += float(qty) * float(px[sym].loc[day])
            if core in holdings and day in voo_s.index:
                eq += float(holdings[core]) * float(voo_s.loc[day])
            return float(eq)

        daily_rets: list[float] = []

        for i, day in enumerate(all_days):
            if day in reb_days or i == 0:
                mom: dict[str, float | None] = {}
                qual: dict[str, float | None] = {}
                for sym, series in px.items():
                    past = series[series.index < day].tail(400)
                    if len(past) < 130:
                        continue
                    tmp = past.reset_index()
                    if tmp.shape[1] != 2:
                        continue
                    tmp.columns = ["time_key", "close"]
                    df_ind = add_indicators(tmp)
                    if "close" not in df_ind.columns:
                        continue
                    m, q = score_from_kline_df(df_ind)
                    mom[sym], qual[sym] = m, q
                ranked = composite_factor_score(momentum_raw=mom, quality_raw=qual, w_mom=0.5, w_qual=0.5)
                picks = [str(x) for x in ranked.head(int(args.n_stock))["symbol"].tolist() if str(x) in px]

                eq_before = mark_to_market(day)
                holdings.clear()
                cash = 0.0
                w_voo = float(args.voo_weight)
                w_stock = max(0.0, 1.0 - w_voo)
                if day not in voo_s.index:
                    continue
                px_v = float(voo_s.loc[day])
                notional_voo = eq_before * w_voo
                holdings[core] = notional_voo / px_v if px_v > 0 else 0.0
                if picks:
                    each = eq_before * (w_stock / float(len(picks)))
                    for sym in picks:
                        if day not in px[sym].index:
                            continue
                        p = float(px[sym].loc[day])
                        holdings[sym] = each / p if p > 0 else 0.0

            eq = mark_to_market(day)
            equity_curve.append(eq)
            if day in voo_s.index:
                voo_only_curve.append(float(voo_qty * float(voo_s.loc[day])))
            elif voo_only_curve:
                voo_only_curve.append(voo_only_curve[-1])
            else:
                voo_only_curve.append(float(args.initial))
            if len(equity_curve) >= 2:
                daily_rets.append((eq / equity_curve[-2]) - 1.0)

        tot = (equity_curve[-1] / equity_curve[0] - 1.0) * 100.0
        years = len(all_days) / 252.0
        ann = ((equity_curve[-1] / equity_curve[0]) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0
        mdd = _max_drawdown_pct(equity_curve)
        sh, so = _sharpe_sortino(daily_rets)
        voo_tot = (voo_only_curve[-1] / voo_only_curve[0] - 1.0) * 100.0

        print("--- Multifactor sleeve backtest (research) ---")
        print(f"Range: {args.start} .. {args.end}  days={len(all_days)}")
        print(f"Strategy total return: {tot:.2f}%  ann~{ann:.2f}%  maxDD={mdd:.2f}%")
        print(f"Sharpe~{sh:.2f}  Sortino~{so:.2f}")
        print(f"VOO buy-hold total return: {voo_tot:.2f}%")
    finally:
        quote_ctx.close()


if __name__ == "__main__":
    main()
