from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderBookSummary:
    bid_total: float
    ask_total: float
    imbalance: float  # (bid-ask)/(bid+ask)
    spread: float
    top_bids: list[tuple[float, float]]  # (price, qty)
    top_asks: list[tuple[float, float]]


def _to_ticker(code: str) -> str:
    # "US.HUT" -> "HUT"
    c = str(code).strip().upper()
    if "." in c:
        return c.split(".")[-1]
    return c


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def fetch_order_book_summary(quote_ctx: Any, code: str, *, levels: int = 5) -> OrderBookSummary | None:
    """
    Best-effort order book snapshot from FutuOpenD.
    Returns None if not available (e.g. permission / session / API differences).
    """
    try:
        from futu import RET_OK, SubType

        # Some OpenD setups require subscription before fetching order book.
        try:
            quote_ctx.subscribe([code], [SubType.ORDER_BOOK], subscribe_push=False)
        except Exception:
            pass

        # Futu API signature: get_order_book(code, num)
        ret, data = quote_ctx.get_order_book(code, num=int(levels))
        if ret != RET_OK or not isinstance(data, dict):
            return None

        bid_rows = data.get("Bid") or data.get("bid") or []
        ask_rows = data.get("Ask") or data.get("ask") or []

        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for r in bid_rows[: int(levels)]:
            bids.append((_safe_float(r.get("price")), _safe_float(r.get("volume", r.get("qty")))))
        for r in ask_rows[: int(levels)]:
            asks.append((_safe_float(r.get("price")), _safe_float(r.get("volume", r.get("qty")))))

        bid_total = sum(q for _, q in bids)
        ask_total = sum(q for _, q in asks)
        denom = bid_total + ask_total
        imbalance = (bid_total - ask_total) / denom if denom > 0 else 0.0

        top_bid = bids[0][0] if bids else 0.0
        top_ask = asks[0][0] if asks else 0.0
        spread = (top_ask - top_bid) if top_ask > 0 and top_bid > 0 else 0.0

        return OrderBookSummary(
            bid_total=bid_total,
            ask_total=ask_total,
            imbalance=imbalance,
            spread=spread,
            top_bids=bids,
            top_asks=asks,
        )
    except Exception:
        return None


@dataclass(frozen=True)
class NewsSummary:
    titles: list[str]
    bearish_hits: list[str]
    bullish_hits: list[str]


_BEARISH_KW = [
    "offering",
    "secondary offering",
    "convertible",
    "dilution",
    "investigation",
    "downgrade",
    "misses",
    "miss",
    "lawsuit",
    "hack",
    "fraud",
    "bankruptcy",
    "sec",
    "delist",
    "guidance cut",
    "cuts guidance",
    "lowers guidance",
    "weak guidance",
    "warning",
    "probe",
    "restatement",
    "halt",
]
_BULLISH_KW = [
    "beats",
    "beat",
    "upgrade",
    "raises guidance",
    "raise guidance",
    "higher guidance",
    "record",
    "partnership",
    "agreement",
    "contract",
    "wins contract",
    "deal",
    "award",
    "order",
    "acquisition",
    "merger",
    "approval",
    "buyback",
    "initiates coverage",
    "price target",
    "bullish",
    "rally",
    "surge",
    "breakout",
    "ai",
    "data center",
]

_IMPACT_KW = [
    # earnings / guidance
    "earnings",
    "guidance",
    "forecast",
    "price target",
    "upgrade",
    "downgrade",
    # corporate actions
    "acquisition",
    "merger",
    "buyback",
    "offering",
    "convertible",
    "deal",
    "partnership",
    "agreement",
    "contract",
    "award",
    # major events
    "sec",
    "investigation",
    "lawsuit",
    "approval",
    "delist",
    "halt",
    "data center",
    "ai",
]


def _kw_hits(titles: list[str], kws: list[str]) -> list[str]:
    hits: list[str] = []
    for t in titles:
        tl = t.lower()
        for kw in kws:
            if kw in tl:
                hits.append(kw)
    # unique, stable order
    out: list[str] = []
    for h in hits:
        if h not in out:
            out.append(h)
    return out


def fetch_news_summary(code: str, *, max_items: int = 5, timeout_s: float = 3.5) -> NewsSummary | None:
    """
    Best-effort public RSS headlines (no API key).
    If blocked/unavailable, returns None.
    """
    if os.getenv("TP_NEWS_RSS_ENABLED", "1").strip() in ("0", "false", "False", "no", "NO"):
        return None
    ticker = _to_ticker(code)
    # Yahoo Finance RSS is usually accessible without auth.
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        r = requests.get(url, timeout=float(timeout_s), headers={"User-Agent": "TradePilot/1.0"})
        if r.status_code != 200 or not r.text:
            return None
        root = ET.fromstring(r.text)
        titles: list[str] = []
        for item in root.findall(".//item"):
            t = item.findtext("title") or ""
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                titles.append(t)
            if len(titles) >= int(max_items):
                break
        if not titles:
            return None
        return NewsSummary(
            titles=titles,
            bearish_hits=_kw_hits(titles, _BEARISH_KW),
            bullish_hits=_kw_hits(titles, _BULLISH_KW),
        )
    except Exception:
        return None


def _load_keyword_weights() -> dict[str, float]:
    """加载由 news_verdict_tracker 生成的历史胜率权重文件。"""
    path = os.path.join("logs", "keyword_weights.json")
    if not os.path.exists(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def classify_news_impact(news: NewsSummary) -> tuple[str, list[str], list[str]]:
    """
    Returns (verdict, bullish_hits, bearish_hits)
    verdict: "利好" | "利空" | "中性"

    动态权重：若 keyword_weights.json 存在，胜率<0.4的关键词从计分中降权；
    胜率>0.7的关键词加权。最终 bull_score > bear_score 则利好，反之利空。
    """
    bull = list(news.bullish_hits or [])
    bear = list(news.bearish_hits or [])

    weights = _load_keyword_weights()

    def _score(kws: list[str]) -> float:
        score = 0.0
        for kw in kws:
            w = weights.get(kw)
            if w is None:
                score += 1.0          # 无历史数据，默认权重1
            elif w >= 0.7:
                score += 1.5          # 历史胜率高，加权
            elif w >= 0.4:
                score += 1.0          # 中等，正常权重
            else:
                score += 0.3          # 历史胜率低，降权
        return score

    if not bull and not bear:
        return "中性", bull, bear

    bull_score = _score(bull)
    bear_score = _score(bear)

    if bull_score > bear_score:
        return "利好", bull, bear
    if bear_score > bull_score:
        return "利空", bull, bear
    return "中性", bull, bear


def extract_impact_titles(news: NewsSummary) -> list[str]:
    """Filter headlines that contain 'impact' keywords."""
    out: list[str] = []
    for t in news.titles or []:
        tl = t.lower()
        if any(kw in tl for kw in _IMPACT_KW):
            out.append(t)
    return out


_translate_cache: dict[str, str] = {}
_translate_cache_loaded = False
_TRANSLATE_CACHE_MAX = 2000


def _translate_cache_path() -> str:
    try:
        from secrets_loader import resolve_runtime_dir

        base = resolve_runtime_dir()
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "logs", "news_translate_cache.json")


def _load_translate_cache() -> None:
    global _translate_cache_loaded
    if _translate_cache_loaded:
        return
    _translate_cache_loaded = True
    path = _translate_cache_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        _translate_cache[k] = v.strip()
    except Exception as e:
        _log.debug("news translate cache load failed: %s", e)


def _save_translate_cache() -> None:
    path = _translate_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        items = list(_translate_cache.items())[-_TRANSLATE_CACHE_MAX:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(items), f, ensure_ascii=False)
    except Exception as e:
        _log.debug("news translate cache save failed: %s", e)


def _needs_zh_translation(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    chinese = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    if chinese >= 4 or (chinese > 0 and chinese / max(len(s), 1) > 0.25):
        return False
    latin = sum(1 for c in s if c.isascii() and c.isalpha())
    return latin >= 8


def _translate_via_mymemory(key: str, *, timeout_s: float) -> str:
    try:
        q = urllib.parse.quote(key[:500])
        url = f"https://api.mymemory.translated.net/get?q={q}&langpair=en|zh-CN"
        r = requests.get(url, timeout=float(timeout_s), headers={"User-Agent": "TradePilot/1.0"})
        if r.status_code != 200:
            return ""
        data = r.json() if r.text else {}
        if not isinstance(data, dict):
            return ""
        details = str(data.get("responseDetails") or "")
        if "MYMEMORY WARNING" in details.upper():
            return ""
        if data.get("responseStatus") != 200:
            return ""
        zh = str((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not zh or zh.lower() == key.lower():
            return ""
        return zh
    except Exception:
        return ""


def _translate_via_google_gtx(key: str, *, timeout_s: float) -> str:
    """Fallback when MyMemory is rate-limited or unreachable."""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": key[:500]},
            timeout=float(timeout_s),
            headers={"User-Agent": "TradePilot/1.0"},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            return ""
        parts = [str(p[0]) for p in data[0] if isinstance(p, list) and p and p[0]]
        zh = "".join(parts).strip()
        if not zh or zh.lower() == key.lower():
            return ""
        return zh
    except Exception:
        return ""


def translate_to_zh(text: str, *, timeout_s: float | None = None) -> str:
    """
    Best-effort EN→ZH for RSS headlines (MyMemory + Google gtx fallback, no API key).
    Returns empty string if disabled, already Chinese, or on failure.
    """
    if os.getenv("TP_NEWS_TRANSLATE_ENABLED", "1").strip() in ("0", "false", "False", "no", "NO"):
        return ""
    _load_translate_cache()
    key = re.sub(r"\s+", " ", (text or "").strip())
    if not key or not _needs_zh_translation(key):
        return ""
    cached = _translate_cache.get(key)
    if cached:
        return cached
    if timeout_s is None:
        timeout_s = float(os.getenv("TP_NEWS_TRANSLATE_TIMEOUT", "8"))

    zh = _translate_via_mymemory(key, timeout_s=float(timeout_s))
    if not zh:
        time.sleep(0.15)
        zh = _translate_via_mymemory(key, timeout_s=float(timeout_s))
    if not zh:
        zh = _translate_via_google_gtx(key, timeout_s=float(timeout_s))

    if not zh:
        _log.info("[NEWS] 标题翻译失败: %s", key[:80])
        return ""

    if len(_translate_cache) >= _TRANSLATE_CACHE_MAX:
        try:
            _translate_cache.pop(next(iter(_translate_cache)))
        except StopIteration:
            pass
    _translate_cache[key] = zh
    _save_translate_cache()
    return zh


def format_news_title_lines(titles: list[str], *, max_items: int = 5) -> list[str]:
    """Format headlines for push: English title + Chinese translation when available."""
    lines: list[str] = []
    translated = 0
    for i, t in enumerate((titles or [])[: int(max_items)], 1):
        title = re.sub(r"\s+", " ", str(t).strip())
        if not title:
            continue
        zh = translate_to_zh(title)
        lines.append(f"{i}. {title}")
        if zh:
            lines.append(f"   译：{zh}")
            translated += 1
        elif _needs_zh_translation(title):
            time.sleep(0.12)
    need = sum(1 for t in (titles or [])[: int(max_items)] if _needs_zh_translation(str(t)))
    if need > 0 and translated == 0:
        _log.warning("[NEWS] 本批 %d 条英文标题均未译出中文，请检查网络或翻译服务", need)
    return lines


def summarize_sell_context(
    *,
    code: str,
    current_price: float,
    buy_price: float,
    pnl_pct: float,
    quote_ctx: Any,
) -> str:
    """
    Produces a short Chinese summary that can be appended to SELL reason.
    """
    levels = int(os.getenv("TP_ORDERBOOK_LEVELS", "5"))
    ob = fetch_order_book_summary(quote_ctx, code, levels=levels)
    news = fetch_news_summary(code, max_items=int(os.getenv("TP_NEWS_RSS_MAX_ITEMS", "5")))

    lines: list[str] = []
    lines.append(f"上下文(辅助判断，不自动):")

    if ob is None:
        lines.append("盘口: N/A")
    else:
        bias = "买盘占优" if ob.imbalance > 0.15 else "卖盘占优" if ob.imbalance < -0.15 else "相对均衡"
        lines.append(
            f"盘口: spread={ob.spread:.4f} | bid={ob.bid_total:.0f} ask={ob.ask_total:.0f} | imbalance={ob.imbalance:+.2f} ({bias})"
        )
        if ob.top_bids and ob.top_asks:
            b1p, b1q = ob.top_bids[0]
            a1p, a1q = ob.top_asks[0]
            lines.append(f"买一: {b1p:.4f}×{b1q:.0f} | 卖一: {a1p:.4f}×{a1q:.0f}")

    if news is None:
        lines.append("舆情/新闻: N/A")
    else:
        bear = ",".join(news.bearish_hits) if news.bearish_hits else "无明显利空关键词"
        bull = ",".join(news.bullish_hits) if news.bullish_hits else "无明显利好关键词"
        lines.append(f"舆情/新闻(近5条标题): 利空={bear}; 利好={bull}")
        lines.extend(format_news_title_lines(news.titles, max_items=5))

    # Simple suggestion text (still manual decision).
    suggest = ""
    if ob is not None and news is not None:
        if ob.imbalance > 0.20 and not news.bearish_hits and pnl_pct < 0:
            suggest = "建议: 盘口偏强且无明显利空，可能是情绪波动；可考虑等待/减仓而非全卖（仍以你的风控为准）。"
        elif ob.imbalance < -0.20 and news.bearish_hits:
            suggest = "建议: 盘口偏弱且有利空关键词，止损/执行卖出更合理。"
    if suggest:
        lines.append(suggest)

    return textwrap.shorten("\n".join(lines), width=1200, placeholder="...")

