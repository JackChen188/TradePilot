from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd


PCF_CACHE_PATH = os.path.join("logs", "fundamental_pcf_cache.json")
MANUAL_FUNDAMENTALS_PATH = os.path.join("config", "fundamentals.json")


@dataclass(frozen=True)
class FundamentalInput:
    code: str
    listing_date: str = ""
    age_days: int | None = None
    pe_ttm: float | None = None
    pcf_ttm: float | None = None
    net_profit: float | None = None
    operating_cash_flow_ttm: float | None = None
    operating_cash_flow_growth_pct: float | None = None


@dataclass(frozen=True)
class FundamentalScore:
    adjustment: int
    size_multiplier: float
    age_days: int | None
    pe_ttm: float | None
    pcf_ttm: float | None
    pe_score: int
    cashflow_score: int
    profit_guard_score: int
    reason: str


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        v = float(value)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"N/A", "NONE", "NULL"}:
        return None
    text = text[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _age_days_from_listing(value: Any) -> int | None:
    d = _parse_date(value)
    if d is None:
        return None
    return max(0, (date.today() - d).days)


def _age_days_from_kline(df: pd.DataFrame | None) -> int | None:
    if df is None or df.empty or "time_key" not in df.columns:
        return None
    try:
        first = str(df["time_key"].iloc[0])[:10]
        d = _parse_date(first)
        if d is None:
            return None
        return max(0, (date.today() - d).days)
    except Exception:
        return None


def _latest_kline_pe(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty or "pe_ratio" not in df.columns:
        return None
    try:
        s = pd.to_numeric(df["pe_ratio"], errors="coerce").dropna()
        if s.empty:
            return None
        return _to_float(s.iloc[-1])
    except Exception:
        return None


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


def _load_manual_fundamentals(path: str = MANUAL_FUNDAMENTALS_PATH) -> dict[str, dict]:
    raw = _load_json(path)
    data = raw.get("symbols", raw)
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for code, row in data.items():
        if isinstance(row, dict):
            out[str(code).strip().upper()] = row
    return out


def _today_key() -> str:
    return date.today().isoformat()


def _load_pcf_cache(path: str = PCF_CACHE_PATH) -> dict[str, dict]:
    raw = _load_json(path)
    if raw.get("date") != _today_key():
        return {}
    data = raw.get("symbols") or {}
    return data if isinstance(data, dict) else {}


def _save_pcf_cache(symbols: dict[str, dict], path: str = PCF_CACHE_PATH) -> None:
    _save_json(path, {"date": _today_key(), "symbols": symbols, "updated_at": datetime.utcnow().isoformat() + "Z"})


def fetch_market_snapshot_fundamentals(quote_ctx: Any, codes: list[str], *, batch_size: int = 200) -> dict[str, dict]:
    from futu import RET_OK

    out: dict[str, dict] = {}
    clean = sorted({str(c).strip().upper() for c in codes if str(c).strip()})
    for i in range(0, len(clean), int(batch_size)):
        batch = clean[i : i + int(batch_size)]
        ret, data = quote_ctx.get_market_snapshot(batch)
        if ret != RET_OK or data is None or data.empty:
            continue
        for _, row in data.iterrows():
            code = str(row.get("code", "") or "").strip().upper()
            if not code:
                continue
            pe = _to_float(row.get("pe_ttm_ratio", None))
            if pe is None:
                pe = _to_float(row.get("pe_ratio", None))
            out[code] = {
                "listing_date": str(row.get("listing_date", "") or "").strip(),
                "age_days": _age_days_from_listing(row.get("listing_date", "")),
                "pe_ttm": pe,
                "net_profit": _to_float(row.get("net_profit", None)),
            }
    return out


def fetch_pcf_cache_for_symbols(
    quote_ctx: Any,
    codes: list[str],
    *,
    enabled: bool,
    max_pages: int,
    page_size: int,
    cache_path: str = PCF_CACHE_PATH,
) -> dict[str, dict]:
    cached = _load_pcf_cache(cache_path)
    target = sorted({str(c).strip().upper() for c in codes if str(c).strip()})
    if not enabled or not target:
        return cached

    missing = {c for c in target if c not in cached}
    if not missing:
        return cached

    try:
        from futu import Market, RET_OK, SimpleFilter, StockField
    except Exception:
        return cached

    filters = []
    for field in (StockField.PE_TTM, StockField.PCF_TTM):
        f = SimpleFilter()
        f.stock_field = field
        f.filter_min = -1e12
        f.filter_max = 1e12
        f.is_no_filter = False
        filters.append(f)

    begin = 0
    pages = 0
    page_size = max(1, int(page_size))
    max_pages = max(0, int(max_pages))
    while missing and pages < max_pages:
        try:
            ret, data = quote_ctx.get_stock_filter(
                market=Market.US,
                filter_list=filters,
                begin=begin,
                num=page_size,
            )
        except Exception:
            break
        if ret != RET_OK:
            break
        last_page, _all_count, items = data
        for item in items:
            code = str(getattr(item, "stock_code", "") or "").strip().upper()
            if code not in missing:
                continue
            cached[code] = {
                "pe_ttm": _to_float(getattr(item, "pe_ttm", None)),
                "pcf_ttm": _to_float(getattr(item, "pcf_ttm", None)),
            }
            missing.discard(code)
        if last_page:
            break
        begin += page_size
        pages += 1
        time.sleep(0.05)

    if cached:
        _save_pcf_cache(cached, cache_path)
    return cached


def build_fundamental_inputs(
    quote_ctx: Any,
    codes: list[str],
    *,
    kline_by_code: dict[str, pd.DataFrame],
    pcf_scan_enabled: bool,
    pcf_scan_max_pages: int,
    pcf_scan_page_size: int,
) -> dict[str, FundamentalInput]:
    snapshot = fetch_market_snapshot_fundamentals(quote_ctx, codes)
    pcf_cache = fetch_pcf_cache_for_symbols(
        quote_ctx,
        codes,
        enabled=bool(pcf_scan_enabled),
        max_pages=int(pcf_scan_max_pages),
        page_size=int(pcf_scan_page_size),
    )
    manual = _load_manual_fundamentals()

    out: dict[str, FundamentalInput] = {}
    for raw_code in codes:
        code = str(raw_code).strip().upper()
        snap = dict(snapshot.get(code, {}))
        cache = dict(pcf_cache.get(code, {}))
        override = dict(manual.get(code, {}))
        row = {**snap, **cache, **override}
        kdf = kline_by_code.get(code)

        pe = _to_float(row.get("pe_ttm", None))
        if pe is None:
            pe = _latest_kline_pe(kdf)

        listing_date = str(row.get("listing_date", "") or "").strip()
        age_days = row.get("age_days", None)
        try:
            age_days = int(age_days) if age_days is not None else None
        except Exception:
            age_days = None
        if age_days is None:
            age_days = _age_days_from_listing(listing_date)
        if age_days is None:
            age_days = _age_days_from_kline(kdf)

        out[code] = FundamentalInput(
            code=code,
            listing_date=listing_date,
            age_days=age_days,
            pe_ttm=pe,
            pcf_ttm=_to_float(row.get("pcf_ttm", None)),
            net_profit=_to_float(row.get("net_profit", None)),
            operating_cash_flow_ttm=_to_float(row.get("operating_cash_flow_ttm", None)),
            operating_cash_flow_growth_pct=_to_float(row.get("operating_cash_flow_growth_pct", None)),
        )
    return out


def _is_young(age_days: int | None) -> bool:
    return age_days is None or int(age_days) < 730


def _score_pe(pe: float | None, age_days: int | None) -> tuple[int, str]:
    if pe is None:
        return 0, "pe=NA"
    if pe <= 0:
        return (0, f"pe={pe:.1f} young/no-penalty") if _is_young(age_days) else (-6, f"pe={pe:.1f} mature-loss")
    if _is_young(age_days):
        if pe <= 80:
            return 2, f"pe={pe:.1f} young-ok"
        if pe <= 120:
            return 0, f"pe={pe:.1f} young-rich"
        return -2, f"pe={pe:.1f} young-very-rich"

    if pe <= 15:
        return 2, f"pe={pe:.1f} cheap"
    if pe <= 45:
        return 5, f"pe={pe:.1f} reasonable"
    if pe <= 80:
        return 2, f"pe={pe:.1f} growth-premium"
    if pe <= 120:
        return -2, f"pe={pe:.1f} expensive"
    return -5, f"pe={pe:.1f} very-expensive"


def _score_cashflow(inp: FundamentalInput) -> tuple[int, str]:
    young = _is_young(inp.age_days)
    if inp.operating_cash_flow_ttm is not None:
        score = 5 if inp.operating_cash_flow_ttm > 0 else (-2 if young else -6)
        parts = [f"ocf_ttm={'pos' if inp.operating_cash_flow_ttm > 0 else 'neg'}"]
        growth = inp.operating_cash_flow_growth_pct
        if growth is not None:
            if growth > 10:
                score += 3
                parts.append(f"ocf_growth={growth:.1f}%")
            elif growth < -20:
                score -= 3
                parts.append(f"ocf_growth={growth:.1f}%")
        return score, ",".join(parts)

    pcf = inp.pcf_ttm
    if pcf is None:
        return 0, "cashflow=NA"
    if pcf <= 0:
        return (-1, f"pcf={pcf:.1f} young-negative") if young else (-5, f"pcf={pcf:.1f} mature-negative")
    if pcf <= 30:
        return 5, f"pcf={pcf:.1f} strong"
    if pcf <= 60:
        return 3, f"pcf={pcf:.1f} ok"
    if pcf <= 100:
        return 0, f"pcf={pcf:.1f} rich"
    return -3, f"pcf={pcf:.1f} very-rich"


def _score_profit_guard(net_profit: float | None, age_days: int | None) -> tuple[int, str]:
    if net_profit is None:
        return 0, "profit=NA"
    if net_profit < 0:
        return (0, "profit=neg young/no-penalty") if _is_young(age_days) else (-3, "profit=neg mature")
    return (1, "profit=pos mature") if not _is_young(age_days) else (0, "profit=pos young")


def _size_multiplier(age_days: int | None, adjustment: int, cashflow_score: int, pe_score: int) -> float:
    if age_days is not None and age_days < 180:
        return 0.30
    if age_days is None or age_days < 730:
        if adjustment < 0 or cashflow_score < 0 or pe_score < 0:
            return 0.60
        return 0.80
    if adjustment <= -8:
        return 0.70
    return 1.00


def score_fundamentals(inp: FundamentalInput, *, max_adjustment: int = 12) -> FundamentalScore:
    pe_score, pe_reason = _score_pe(inp.pe_ttm, inp.age_days)
    cashflow_score, cashflow_reason = _score_cashflow(inp)
    profit_guard_score, profit_reason = _score_profit_guard(inp.net_profit, inp.age_days)
    raw = int(pe_score + cashflow_score + profit_guard_score)
    cap = max(0, int(max_adjustment))
    adjustment = max(-cap, min(cap, raw))
    multiplier = _size_multiplier(inp.age_days, adjustment, cashflow_score, pe_score)
    age_text = "age=NA" if inp.age_days is None else f"age={int(inp.age_days)}d"
    reason = (
        f"fundamental adj={adjustment:+d} {age_text}; "
        f"{pe_reason}; {cashflow_reason}; {profit_reason}; size={multiplier:.2f}"
    )
    return FundamentalScore(
        adjustment=int(adjustment),
        size_multiplier=float(multiplier),
        age_days=inp.age_days,
        pe_ttm=inp.pe_ttm,
        pcf_ttm=inp.pcf_ttm,
        pe_score=int(pe_score),
        cashflow_score=int(cashflow_score),
        profit_guard_score=int(profit_guard_score),
        reason=reason,
    )
