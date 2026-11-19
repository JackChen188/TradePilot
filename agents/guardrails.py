from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


LLM_BUDGET_STATE_PATH = os.path.join("logs", "agent_llm_budget_state.json")


def _today_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _load(path: str) -> dict[str, Any]:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def llm_call_allowed(*, max_calls_per_day: int, path: str = LLM_BUDGET_STATE_PATH) -> tuple[bool, str]:
    if int(max_calls_per_day) <= 0:
        return False, "llm_daily_budget_zero"
    state = _load(path)
    today = _today_utc()
    if state.get("date") != today:
        state = {"date": today, "calls": 0}
    calls = int(state.get("calls", 0) or 0)
    if calls >= int(max_calls_per_day):
        return False, f"llm_daily_budget_exhausted:{calls}/{int(max_calls_per_day)}"
    return True, ""


def record_llm_call(path: str = LLM_BUDGET_STATE_PATH) -> None:
    state = _load(path)
    today = _today_utc()
    if state.get("date") != today:
        state = {"date": today, "calls": 0}
    state["calls"] = int(state.get("calls", 0) or 0) + 1
    state["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    _save(path, state)
