"""
Offline monthly (or ad-hoc) Alpha universe refresh.

1) Try Futu ``get_plate_stock`` for ``TP_SP500_PLATE`` (default US.SPX500 — adjust to your OpenD plate code).
2) Fallback: ``config/sp500_universe.json`` ``symbols`` array.
3) Exclude ``TP_CORE_SYMBOL`` (default US.VOO).
4) Score with ``alpha_multifactor`` (50% momentum 6m ex 1m, 50% price-based quality proxy).
5) Keep top ``--top-frac`` (default 0.2), cap ``--max-symbols`` (default 35), optional vol filter.

Writes ``config/alpha_factor_universe.json`` consumed by ``main._alpha_cycle_symbols``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone

import pandas as pd
from futu import OpenQuoteContext, RET_OK

from alpha_multifactor import (
    composite_factor_score,
    load_symbol_list_json,
    save_symbol_list_json,
    score_from_kline_df,
    top_fraction_symbols,
)
from config import FUTU, TRADE
from data_provider import fetch_daily_kline
from indicators import add_indicators


def _plate_symbols(quote_ctx: OpenQuoteContext, plate: str) -> list[str]:
    try:
        ret, df = quote_ctx.get_plate_stock(str(plate))
        if ret != RET_OK or df is None or df.empty or "code" not in df.columns:
            return []
        return [str(x).strip().upper() for x in df["code"].tolist() if str(x).strip()]
    except Exception:
        return []


def _annualized_vol_last(df: pd.DataFrame, window: int = 63) -> float | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    s = pd.to_numeric(df["close"], errors="coerce").iloc[-window:]
    if len(s) < max(20, window // 2):
        return None
    lr = (s / s.shift(1)).apply(lambda x: math.log(float(x)) if x and float(x) > 0 else float("nan")).dropna()
    if len(lr) < 15:
        return None
    daily = float(lr.std(ddof=1))
    return float(daily * math.sqrt(252.0)) * 100.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plate", type=str, default=os.getenv("TP_SP500_PLATE", "US.SPX500"))
    ap.add_argument("--top-frac", type=float, default=float(os.getenv("TP_UNIVERSE_TOP_FRAC", "0.2")))
    ap.add_argument("--max-symbols", type=int, default=int(os.getenv("TP_UNIVERSE_MAX_SYMBOLS", "35")))
    ap.add_argument("--days", type=int, default=900)
    ap.add_argument("--out", type=str, default=os.path.join("config", "alpha_factor_universe.json"))
    ap.add_argument("--fallback-json", type=str, default=os.path.join("config", "sp500_universe.json"))
    args = ap.parse_args()

    core = str(TRADE.core_symbol).strip().upper()
    vol_cap = float(os.getenv("TP_ENHANCEMENT_VOL_CAP_ANN_PCT", "120"))

    quote_ctx = OpenQuoteContext(host=FUTU.host, port=FUTU.port)
    try:
        syms = _plate_symbols(quote_ctx, args.plate)
        if not syms:
            syms = load_symbol_list_json(args.fallback_json)
        syms = sorted({s.upper() for s in syms if s.upper() != core})

        momentum_raw: dict[str, float | None] = {}
        quality_raw: dict[str, float | None] = {}

        for code in syms:
            try:
                raw = fetch_daily_kline(quote_ctx, code, days=int(args.days))
                df = add_indicators(raw)
                ann_vol = _annualized_vol_last(df, window=63)
                if ann_vol is not None and ann_vol > vol_cap:
                    continue
                mom, qual = score_from_kline_df(df)
                momentum_raw[code] = mom
                quality_raw[code] = qual
            except Exception:
                continue

        scored = composite_factor_score(momentum_raw=momentum_raw, quality_raw=quality_raw, w_mom=0.5, w_qual=0.5)
        picked = top_fraction_symbols(scored, top_frac=float(args.top_frac))
        picked = picked[: int(args.max_symbols)]

        save_symbol_list_json(
            args.out,
            picked,
            meta={
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "refresh_alpha_universe",
                "plate": args.plate,
                "top_frac": float(args.top_frac),
                "max_symbols": int(args.max_symbols),
                "input_count": len(syms),
                "scored_count": len(scored),
            },
        )
        print(f"Wrote {len(picked)} symbols -> {args.out}")
    finally:
        quote_ctx.close()


if __name__ == "__main__":
    main()
