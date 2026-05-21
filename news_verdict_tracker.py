"""
news_verdict_tracker.py
-----------------------
闭环反馈系统：追踪新闻舆情推送的「利好/利空」信号准确率，并自动反馈优化关键词权重。

== 数据流 ==

1. 每次新闻推送「利好/利空」时：
   log_verdict(code, verdict, price_at_push, bull_keywords, bear_keywords)
   → 写入 logs/news_verdict_log.csv（含触发的关键词）

2. 每天定时评估已推送7天以上、尚未评估的记录：
   evaluate_pending_outcomes()
   → 获取当前价格，计算涨跌幅
   → 判断 outcome: correct / incorrect
   → 写回 CSV（price_eval, pct_change, outcome）

3. 每周一发送周报：
   build_weekly_verdict_push()
   → 本周新信号及走势
   → 历史累计准确率（利好/利空分开统计）
   → 最佳/最差关键词排行
   → 对比上周准确率（是否在进步）

4. 每天评估后自动更新关键词权重：
   update_keyword_weights()
   → 读取所有 correct/incorrect 记录
   → 计算每个关键词的历史胜率
   → 写入 logs/keyword_weights.json
   → market_context.py 读取此文件动态调整判断

== 文件 ==
  logs/news_verdict_log.csv        主日志
  logs/keyword_weights.json        关键词权重（由本模块生成）
  logs/weekly_verdict_push_state.json  周报去重状态
"""

from __future__ import annotations

import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

VERDICT_LOG_PATH = os.path.join("logs", "news_verdict_log.csv")
KEYWORD_WEIGHTS_PATH = os.path.join("logs", "keyword_weights.json")
WEEKLY_PUSH_STATE_PATH = os.path.join("logs", "weekly_verdict_push_state.json")

_LOOKBACK_DAYS = 28       # 周报回溯窗口
_OUTCOME_EVAL_DAYS = 7    # 推送后多少天评估结果
_MIN_PCT_CORRECT = 3.0    # 涨跌幅超过此阈值才算「有效」信号（过滤噪音）

_LOG_FIELDS = [
    "ts_utc",         # 推送时间
    "code",           # 股票代码
    "verdict",        # 利好 / 利空
    "price_at_push",  # 推送时价格
    "bull_keywords",  # 触发的利好关键词（逗号分隔）
    "bear_keywords",  # 触发的利空关键词（逗号分隔）
    "price_eval",     # 评估时价格（7天后填入）
    "pct_change",     # 涨跌幅%
    "outcome",        # correct / incorrect / pending / noise
]


# ---------------------------------------------------------------------------
# Yahoo Finance 价格获取
# ---------------------------------------------------------------------------

def _fetch_yahoo_price(ticker: str, timeout_s: float = 4.0) -> float | None:
    sym = ticker.strip().upper()
    if "." in sym:
        sym = sym.split(".")[-1]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
    try:
        r = requests.get(url, timeout=float(timeout_s), headers={"User-Agent": "TradePilot/1.0"})
        if r.status_code != 200:
            return None
        meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 记录推送日志
# ---------------------------------------------------------------------------

def log_verdict(
    code: str,
    verdict: str,
    price_at_push: float,
    bull_keywords: list[str] | None = None,
    bear_keywords: list[str] | None = None,
) -> None:
    """推送后立即调用，记录信号到 CSV。"""
    if verdict not in ("利好", "利空"):
        return
    os.makedirs(os.path.dirname(VERDICT_LOG_PATH), exist_ok=True)
    exists = os.path.exists(VERDICT_LOG_PATH) and os.path.getsize(VERDICT_LOG_PATH) > 0
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bull_s = ",".join(bull_keywords or [])
    bear_s = ",".join(bear_keywords or [])
    with open(VERDICT_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({
            "ts_utc": ts,
            "code": code,
            "verdict": verdict,
            "price_at_push": f"{price_at_push:.4f}",
            "bull_keywords": bull_s,
            "bear_keywords": bear_s,
            "price_eval": "",
            "pct_change": "",
            "outcome": "pending",
        })


# ---------------------------------------------------------------------------
# 读写 CSV
# ---------------------------------------------------------------------------

def _load_all_rows() -> list[dict]:
    if not os.path.exists(VERDICT_LOG_PATH):
        return []
    try:
        with open(VERDICT_LOG_PATH, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _save_all_rows(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(VERDICT_LOG_PATH), exist_ok=True)
    with open(VERDICT_LOG_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        w.writeheader()
        for row in rows:
            # 确保所有字段都存在（向前兼容旧记录）
            w.writerow({k: row.get(k, "") for k in _LOG_FIELDS})


# ---------------------------------------------------------------------------
# 评估 pending 记录的 outcome
# ---------------------------------------------------------------------------

def evaluate_pending_outcomes() -> int:
    """
    检查所有 outcome=pending 且推送超过 _OUTCOME_EVAL_DAYS 天的记录，
    获取当前价格，写入 outcome。返回评估了多少条。
    """
    rows = _load_all_rows()
    cutoff = datetime.now(timezone.utc) - timedelta(days=_OUTCOME_EVAL_DAYS)
    updated = 0
    for row in rows:
        if row.get("outcome", "pending") != "pending":
            continue
        try:
            ts = datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts > cutoff:
            continue  # 还没到评估时间
        code = row.get("code", "").strip().upper()
        verdict = row.get("verdict", "").strip()
        try:
            price_push = float(row.get("price_at_push", 0) or 0)
        except Exception:
            price_push = 0.0
        if not code or price_push <= 0:
            row["outcome"] = "invalid"
            updated += 1
            continue

        cur = _fetch_yahoo_price(code)
        time.sleep(0.3)
        if cur is None or cur <= 0:
            continue  # 获取失败，下次再试

        pct = (cur - price_push) / price_push * 100
        row["price_eval"] = f"{cur:.4f}"
        row["pct_change"] = f"{pct:.2f}"

        if abs(pct) < _MIN_PCT_CORRECT:
            row["outcome"] = "noise"  # 涨跌幅太小，不计入准确率
        elif verdict == "利好":
            row["outcome"] = "correct" if pct > 0 else "incorrect"
        else:  # 利空
            row["outcome"] = "correct" if pct < 0 else "incorrect"
        updated += 1

    if updated:
        _save_all_rows(rows)
        update_keyword_weights(rows)
    return updated


# ---------------------------------------------------------------------------
# 关键词权重更新
# ---------------------------------------------------------------------------

def update_keyword_weights(rows: list[dict] | None = None) -> dict:
    """
    统计每个关键词的历史胜率，写入 keyword_weights.json。
    格式: {"ai": 0.75, "upgrade": 0.82, ...}
    胜率 = correct / (correct + incorrect)，样本<3则不更新（数据不足）。
    """
    if rows is None:
        rows = _load_all_rows()

    # kw → {correct: int, incorrect: int}
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "incorrect": 0})

    for row in rows:
        outcome = row.get("outcome", "")
        if outcome not in ("correct", "incorrect"):
            continue
        verdict = row.get("verdict", "")
        # 对利好信号，分析 bull_keywords；对利空信号，分析 bear_keywords
        if verdict == "利好":
            kws = [k.strip() for k in row.get("bull_keywords", "").split(",") if k.strip()]
        else:
            kws = [k.strip() for k in row.get("bear_keywords", "").split(",") if k.strip()]
        for kw in kws:
            stats[kw][outcome] += 1

    weights: dict[str, float] = {}
    for kw, s in stats.items():
        total = s["correct"] + s["incorrect"]
        if total >= 3:  # 样本至少3次才置信
            weights[kw] = round(s["correct"] / total, 3)

    # 读取现有权重，合并（保留已有、只更新有新数据的）
    existing = _load_keyword_weights()
    existing.update(weights)

    os.makedirs(os.path.dirname(KEYWORD_WEIGHTS_PATH), exist_ok=True)
    with open(KEYWORD_WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    return existing


def _load_keyword_weights() -> dict[str, float]:
    if not os.path.exists(KEYWORD_WEIGHTS_PATH):
        return {}
    try:
        with open(KEYWORD_WEIGHTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 准确率统计
# ---------------------------------------------------------------------------

def _calc_accuracy_stats(rows: list[dict], since_ts: datetime | None = None) -> dict:
    """
    返回:
      total_signals, correct, incorrect, noise, accuracy_pct
      top_correct_kws: [(kw, win_rate, n), ...]
      top_wrong_kws:   [(kw, win_rate, n), ...]
    """
    kw_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "incorrect": 0})
    total = correct = incorrect = noise = 0

    for row in rows:
        outcome = row.get("outcome", "pending")
        if outcome == "pending" or outcome == "invalid":
            continue
        try:
            ts = datetime.strptime(row["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if since_ts and ts < since_ts:
            continue

        total += 1
        if outcome == "correct":
            correct += 1
        elif outcome == "incorrect":
            incorrect += 1
        elif outcome == "noise":
            noise += 1

        # 关键词统计
        verdict = row.get("verdict", "")
        if verdict == "利好":
            kws = [k.strip() for k in row.get("bull_keywords", "").split(",") if k.strip()]
        else:
            kws = [k.strip() for k in row.get("bear_keywords", "").split(",") if k.strip()]
        for kw in kws:
            if outcome in ("correct", "incorrect"):
                kw_stats[kw][outcome] += 1

    evaluated = correct + incorrect
    accuracy_pct = round(correct / evaluated * 100, 1) if evaluated > 0 else None

    # 关键词排行（至少2次样本）
    kw_rates = []
    for kw, s in kw_stats.items():
        n = s["correct"] + s["incorrect"]
        if n >= 2:
            kw_rates.append((kw, round(s["correct"] / n * 100, 1), n))
    kw_rates.sort(key=lambda x: (-x[1], -x[2]))

    return {
        "total_signals": total,
        "correct": correct,
        "incorrect": incorrect,
        "noise": noise,
        "evaluated": evaluated,
        "accuracy_pct": accuracy_pct,
        "top_correct_kws": kw_rates[:5],
        "top_wrong_kws": list(reversed(kw_rates))[:5],
    }


# ---------------------------------------------------------------------------
# 周报触发状态
# ---------------------------------------------------------------------------

def _load_weekly_state() -> dict:
    if not os.path.exists(WEEKLY_PUSH_STATE_PATH):
        return {}
    try:
        with open(WEEKLY_PUSH_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_weekly_state(state: dict) -> None:
    os.makedirs(os.path.dirname(WEEKLY_PUSH_STATE_PATH), exist_ok=True)
    with open(WEEKLY_PUSH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_send_weekly_review_now(target_weekday: int = 0, target_hhmm: str = "09:00") -> bool:
    state = _load_weekly_state()
    now_utc = datetime.now(timezone.utc)
    iso_week = list(now_utc.isocalendar()[:2])
    if state.get("last_sent_iso_week") == iso_week:
        return False
    if now_utc.weekday() != target_weekday:
        return False
    return now_utc.strftime("%H:%M") >= target_hhmm


def mark_weekly_review_sent(accuracy_pct: float | None = None) -> None:
    state = _load_weekly_state()
    now_utc = datetime.now(timezone.utc)
    state["last_sent_iso_week"] = list(now_utc.isocalendar()[:2])
    state["last_sent_ts"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if accuracy_pct is not None:
        # 保存上周准确率，下周用来对比
        state["last_week_accuracy_pct"] = accuracy_pct
    _save_weekly_state(state)


# ---------------------------------------------------------------------------
# 生成周报内容
# ---------------------------------------------------------------------------

def build_weekly_verdict_push(lookback_days: int = _LOOKBACK_DAYS) -> str:
    rows = _load_all_rows()
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=lookback_days)
    recent_rows = [r for r in rows if _parse_ts(r.get("ts_utc", "")) and _parse_ts(r["ts_utc"]) >= cutoff]

    now_str = now_utc.strftime("%Y-%m-%d")
    lines = [f"📊 TradePilot 舆情追踪周报 ({now_str})", ""]

    # ── 本期信号列表 ──
    lines.append(f"【本期信号追踪（过去{lookback_days}天）】")
    if not recent_rows:
        lines.append("  暂无记录")
    else:
        # 每只股票取最早一条
        seen: dict[str, dict] = {}
        for row in recent_rows:
            c = row.get("code", "").strip().upper()
            if c and c not in seen:
                seen[c] = row

        for code, row in seen.items():
            verdict = row.get("verdict", "")
            ts_str = str(row.get("ts_utc", ""))[:10]
            try:
                price_push = float(row.get("price_at_push") or 0)
            except Exception:
                price_push = 0.0
            outcome = row.get("outcome", "pending")
            pct_s = row.get("pct_change", "")

            if outcome == "pending":
                # 实时拉取价格
                cur = _fetch_yahoo_price(code)
                time.sleep(0.3)
                if cur and price_push > 0:
                    pct = (cur - price_push) / price_push * 100
                    trend = f"${price_push:.2f}→${cur:.2f} {'▲' if pct>=0 else '▼'}{pct:+.1f}%  ⏳待评估"
                else:
                    trend = f"${price_push:.2f}→价格获取中  ⏳待评估"
            elif outcome == "correct":
                trend = f"${price_push:.2f}→${row.get('price_eval','?')}  {pct_s}%  ✅正确"
            elif outcome == "incorrect":
                trend = f"${price_push:.2f}→${row.get('price_eval','?')}  {pct_s}%  ❌错误"
            elif outcome == "noise":
                trend = f"${price_push:.2f}→${row.get('price_eval','?')}  {pct_s}%  ➖微幅波动"
            else:
                trend = f"数据不足"

            icon = "🟢" if verdict == "利好" else "🔴"
            lines.append(f"  {icon} {code}  [{ts_str}]  {verdict}  {trend}")

    lines.append("")

    # ── 历史累计准确率 ──
    all_stats = _calc_accuracy_stats(rows)
    lines.append("【历史累计准确率（全部记录）】")
    if all_stats["evaluated"] == 0:
        lines.append("  暂无已评估记录（信号需推送7天后才评估）")
    else:
        acc = all_stats["accuracy_pct"]
        lines.append(f"  总信号: {all_stats['total_signals']}  已评估: {all_stats['evaluated']}")
        lines.append(f"  正确: {all_stats['correct']}  错误: {all_stats['incorrect']}  噪音: {all_stats['noise']}")
        lines.append(f"  准确率: {acc}%" if acc is not None else "  准确率: 计算中")

        # 对比上周
        state = _load_weekly_state()
        last_acc = state.get("last_week_accuracy_pct")
        if last_acc is not None and acc is not None:
            delta = acc - last_acc
            trend_str = f"{'↑' if delta >= 0 else '↓'}{abs(delta):.1f}% vs上周({last_acc}%)"
            lines.append(f"  趋势: {trend_str}")

    lines.append("")

    # ── 关键词准确率排行 ──
    if all_stats["evaluated"] >= 5:
        lines.append("【关键词准确率 Top5（样本≥2次）】")
        if all_stats["top_correct_kws"]:
            best = "  ".join([f"{kw}({rate}%/{n}次)" for kw, rate, n in all_stats["top_correct_kws"]])
            lines.append(f"  最准: {best}")
        if all_stats["top_wrong_kws"]:
            worst = "  ".join([f"{kw}({rate}%/{n}次)" for kw, rate, n in all_stats["top_wrong_kws"][:3]])
            lines.append(f"  最差: {worst}")
        lines.append("")

    # ── 自我评价 ──
    acc = all_stats.get("accuracy_pct")
    if acc is not None:
        if acc >= 70:
            verdict_self = "✅ 信号质量良好，关键词识别有效"
        elif acc >= 55:
            verdict_self = "⚠️ 信号质量一般，需观察更多样本"
        else:
            verdict_self = "❌ 准确率偏低，关键词权重已自动调整"
        lines.append(f"【系统自评】{verdict_self}")
        lines.append("")

    lines.append("注：outcome 在信号推送7天后自动评估，价格来自 Yahoo Finance。")
    return "\n".join(lines)


def _parse_ts(ts_str: str) -> datetime | None:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
