from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import pandas as pd

from config import STRATEGY
from data_provider import fetch_daily_kline


RECOMMENDATION_LOG_PATH = os.path.join("logs", "recommendation_log.csv")
RECOMMENDATION_OUTCOME_PATH = os.path.join("logs", "recommendation_outcomes.csv")
RECOMMENDATION_REVIEW_STATE_PATH = os.path.join("logs", "recommendation_review_state.json")

RECOMMENDATION_FIELDS = [
    "rec_id",
    "ts_utc",
    "date_local",
    "code",
    "rank",
    "score",
    "rank_pct",
    "price_at_recommend",
    "benchmark_code",
    "benchmark_price_at_recommend",
    "reason",
]

OUTCOME_FIELDS = [
    "rec_id",
    "code",
    "date_local",
    "horizon_trading_days",
    "price_at_recommend",
    "price_eval",
    "pct_change",
    "benchmark_code",
    "benchmark_price_at_recommend",
    "benchmark_price_eval",
    "benchmark_pct_change",
    "excess_pct",
    "outcome",
    "evaluated_at_utc",
]

DEFAULT_HORIZONS = (1, 3, 5, 10, 20)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_local() -> str:
    tz_name = (getattr(STRATEGY, "display_timezone", "") or "Europe/Berlin").strip()
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _ensure_csv(path: str, fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()


def _read_csv(path: str, fields: list[str]) -> list[dict]:
    _ensure_csv(path, fields)
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_rows(path: str, fields: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    _ensure_csv(path, fields)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _rec_id(date_local: str, code: str) -> str:
    return f"{date_local}:{code.strip().upper()}"


def log_daily_recommendations(
    candidates: list[dict],
    *,
    benchmark_code: str,
    benchmark_price: float,
    top_n: int = 5,
) -> int:
    """
    Record each symbol once per local day when it appears in the top recommendation set.
    Returns number of new recommendation rows written.
    """
    if not candidates:
        return 0

    date_local = _today_local()
    existing = _read_csv(RECOMMENDATION_LOG_PATH, RECOMMENDATION_FIELDS)
    seen_ids = {str(r.get("rec_id", "")) for r in existing}
    ranked = sorted(candidates, key=lambda x: (-int(x.get("score", 0)), -float(x.get("rank_pct", 0.0)), str(x.get("code", ""))))

    rows: list[dict] = []
    for idx, item in enumerate(ranked[: int(top_n)], start=1):
        code = str(item.get("code", "") or "").strip().upper()
        if not code:
            continue
        rid = _rec_id(date_local, code)
        if rid in seen_ids:
            continue
        ar = item.get("ar")
        reason = str(getattr(ar, "reason", "") or item.get("reason", "") or "")
        rows.append(
            {
                "rec_id": rid,
                "ts_utc": _now_utc(),
                "date_local": date_local,
                "code": code,
                "rank": idx,
                "score": int(item.get("score", getattr(ar, "score", 0) or 0)),
                "rank_pct": f"{float(item.get('rank_pct', 0.0)) * 100.0:.2f}",
                "price_at_recommend": f"{float(item.get('current', 0.0) or 0.0):.4f}",
                "benchmark_code": str(benchmark_code).strip().upper(),
                "benchmark_price_at_recommend": f"{float(benchmark_price):.4f}",
                "reason": reason[:1000],
            }
        )
    _append_rows(RECOMMENDATION_LOG_PATH, RECOMMENDATION_FIELDS, rows)
    return len(rows)


def _prepare_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "time_key" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["time_key"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["trade_date"]).reset_index(drop=True)
    return out


def _price_after_horizon(df: pd.DataFrame, rec_date: str, horizon: int) -> float | None:
    if df.empty or "close" not in df.columns:
        return None
    idxs = df.index[df["trade_date"] >= rec_date].tolist()
    if not idxs:
        return None
    target_idx = int(idxs[0]) + int(horizon)
    if target_idx >= len(df):
        return None
    try:
        return float(df.iloc[target_idx]["close"])
    except Exception:
        return None


def evaluate_recommendation_outcomes(
    quote_ctx: Any,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    min_abs_move_pct: float = 0.0,
) -> int:
    recs = _read_csv(RECOMMENDATION_LOG_PATH, RECOMMENDATION_FIELDS)
    outcomes = _read_csv(RECOMMENDATION_OUTCOME_PATH, OUTCOME_FIELDS)
    done = {(r.get("rec_id", ""), str(r.get("horizon_trading_days", ""))) for r in outcomes}
    pending = [
        r
        for r in recs
        for h in horizons
        if (r.get("rec_id", ""), str(int(h))) not in done
    ]
    if not pending:
        return 0

    codes = sorted({str(r.get("code", "")).strip().upper() for r in recs if str(r.get("code", "")).strip()})
    benchmarks = sorted({str(r.get("benchmark_code", "")).strip().upper() for r in recs if str(r.get("benchmark_code", "")).strip()})
    data: dict[str, pd.DataFrame] = {}
    for code in sorted(set(codes + benchmarks)):
        try:
            data[code] = _prepare_daily(fetch_daily_kline(quote_ctx, code, days=120))
        except Exception:
            data[code] = pd.DataFrame()

    rows: list[dict] = []
    for rec in recs:
        code = str(rec.get("code", "")).strip().upper()
        bcode = str(rec.get("benchmark_code", "")).strip().upper()
        rec_date = str(rec.get("date_local", "")).strip()
        rec_price = _to_float(rec.get("price_at_recommend", 0))
        bench_rec_price = _to_float(rec.get("benchmark_price_at_recommend", 0))
        if not code or not rec_date or rec_price <= 0:
            continue
        for horizon in horizons:
            if (rec.get("rec_id", ""), str(int(horizon))) in done:
                continue
            px = _price_after_horizon(data.get(code, pd.DataFrame()), rec_date, int(horizon))
            bpx = _price_after_horizon(data.get(bcode, pd.DataFrame()), rec_date, int(horizon)) if bcode else None
            if px is None:
                continue
            pct = (float(px) / float(rec_price) - 1.0) * 100.0
            bpct = (float(bpx) / float(bench_rec_price) - 1.0) * 100.0 if bpx and bench_rec_price > 0 else 0.0
            excess = pct - bpct
            if abs(pct) <= float(min_abs_move_pct):
                outcome = "noise"
            elif excess > 0:
                outcome = "outperform"
            else:
                outcome = "underperform"
            rows.append(
                {
                    "rec_id": rec.get("rec_id", ""),
                    "code": code,
                    "date_local": rec_date,
                    "horizon_trading_days": int(horizon),
                    "price_at_recommend": f"{rec_price:.4f}",
                    "price_eval": f"{float(px):.4f}",
                    "pct_change": f"{pct:.2f}",
                    "benchmark_code": bcode,
                    "benchmark_price_at_recommend": f"{bench_rec_price:.4f}",
                    "benchmark_price_eval": f"{float(bpx):.4f}" if bpx else "",
                    "benchmark_pct_change": f"{bpct:.2f}",
                    "excess_pct": f"{excess:.2f}",
                    "outcome": outcome,
                    "evaluated_at_utc": _now_utc(),
                }
            )
    _append_rows(RECOMMENDATION_OUTCOME_PATH, OUTCOME_FIELDS, rows)
    return len(rows)


def build_recommendation_review_text(*, lookback_days: int = 90) -> str:
    rows = _read_csv(RECOMMENDATION_OUTCOME_PATH, OUTCOME_FIELDS)
    cutoff = datetime.now(timezone.utc).timestamp() - int(lookback_days) * 86400
    recent = []
    for r in rows:
        try:
            ts = datetime.strptime(str(r.get("evaluated_at_utc", "")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if ts.timestamp() >= cutoff:
                recent.append(r)
        except Exception:
            continue
    if not recent:
        return "推荐复盘：暂无已完成评估。"

    lines = [f"推荐复盘（近{int(lookback_days)}天评估样本）"]
    for horizon in DEFAULT_HORIZONS:
        hs = [r for r in recent if int(float(r.get("horizon_trading_days", 0) or 0)) == int(horizon)]
        if not hs:
            continue
        valid = [r for r in hs if r.get("outcome") in ("outperform", "underperform")]
        if not valid:
            continue
        wins = sum(1 for r in valid if r.get("outcome") == "outperform")
        avg_ret = sum(_to_float(r.get("pct_change")) for r in valid) / len(valid)
        avg_excess = sum(_to_float(r.get("excess_pct")) for r in valid) / len(valid)
        lines.append(
            f"{horizon}日: 样本={len(valid)} 跑赢VOO={wins / len(valid) * 100.0:.1f}% "
            f"平均收益={avg_ret:.2f}% 平均超额={avg_excess:.2f}%"
        )
    return "\n".join(lines)


def should_evaluate_recommendations_today() -> bool:
    state = {}
    try:
        if os.path.exists(RECOMMENDATION_REVIEW_STATE_PATH):
            with open(RECOMMENDATION_REVIEW_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f) or {}
    except Exception:
        state = {}
    return state.get("last_eval_date") != _today_local()


def mark_recommendations_evaluated_today(updated: int) -> None:
    os.makedirs(os.path.dirname(RECOMMENDATION_REVIEW_STATE_PATH), exist_ok=True)
    with open(RECOMMENDATION_REVIEW_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "last_eval_date": _today_local(),
                "last_eval_at_utc": _now_utc(),
                "updated": int(updated),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
