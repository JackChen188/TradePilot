from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from recommendation_review import OUTCOME_FIELDS, RECOMMENDATION_OUTCOME_PATH


AGENT_MEMORY_PATH = os.path.join("logs", "agent_decision_memory.jsonl")


def _read_csv_rows(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    import csv

    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def get_recent_memory_context(code: str, *, limit: int = 3) -> list[dict[str, Any]]:
    code_u = str(code).strip().upper()
    rows = [r for r in _read_csv_rows(RECOMMENDATION_OUTCOME_PATH) if str(r.get("code", "")).upper() == code_u]
    rows = rows[-30:]
    grouped: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        grouped.setdefault(str(r.get("rec_id", "")), []).append(r)

    out: list[dict[str, Any]] = []
    for rec_id, items in reversed(list(grouped.items())):
        horizons = []
        for r in sorted(items, key=lambda x: int(float(x.get("horizon_trading_days", 0) or 0))):
            horizons.append(
                {
                    "days": int(float(r.get("horizon_trading_days", 0) or 0)),
                    "pct_change": r.get("pct_change", ""),
                    "excess_pct": r.get("excess_pct", ""),
                    "outcome": r.get("outcome", ""),
                }
            )
        out.append({"rec_id": rec_id, "horizons": horizons})
        if len(out) >= int(limit):
            break
    return out


def append_decision_memory(entry: dict[str, Any], path: str = AGENT_MEMORY_PATH) -> None:
    ctx = entry.get("context", {})
    review = entry.get("review", {})
    row = {
        "created_at": entry.get("created_at") or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_local": entry.get("date_local", ""),
        "code": ctx.get("code", ""),
        "decision": review.get("decision", ""),
        "portfolio_action": (entry.get("portfolio_manager") or {}).get("final_action", ""),
        "confidence": review.get("confidence", 0),
        "risk_flags": review.get("risk_flags", []),
        "strategy": ctx.get("strategy", {}),
        "memory_context": ctx.get("memory_context", []),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

