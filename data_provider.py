from __future__ import annotations

import time
import threading
from collections import deque
from datetime import date, timedelta
from typing import Any

import pandas as pd

DATA_PROVIDER_VERSION = "2026-05-19b"

# ---------------------------------------------------------------------------
# Futu kline rate-limiter: max 60 requests per 30 s (leave headroom → 30).
# Thread-safe; shared across all callers in the same process.
# _KLINE_MIN_INTERVAL_S adds a mandatory floor between consecutive calls to
# prevent micro-bursts that can still trip the server-side counter.
# ---------------------------------------------------------------------------
_KLINE_WINDOW_S = 30
_KLINE_MAX_IN_WINDOW = 30          # conservative cap (Futu hard limit is 60)
_KLINE_MIN_INTERVAL_S = 0.8        # min gap between consecutive kline calls
_kline_lock = threading.Lock()
_kline_ts: deque[float] = deque()  # timestamps of recent kline requests
_kline_last_call_ts: float = 0.0   # monotonic time of last call (for min interval)

# ---------------------------------------------------------------------------
# Daily kline in-memory cache.
# Same stock same day is fetched only ONCE; all subsequent calls within the
# trading day return the cached DataFrame, eliminating redundant API calls
# that are the primary cause of 频率太高 errors.
# Cache key: "CODE:start:end"; expires at 23:59:59 local time of the request day.
# ---------------------------------------------------------------------------
_kline_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_kline_cache_lock = threading.Lock()


def _kline_cache_key(code: str, start: str, end: str) -> str:
    return f"{code}:{start}:{end}"


def _kline_cache_get(code: str, start: str, end: str) -> "pd.DataFrame | None":
    key = _kline_cache_key(code, start, end)
    with _kline_cache_lock:
        entry = _kline_cache.get(key)
    if entry is None:
        return None
    expires_at, df = entry
    if time.time() > expires_at:
        with _kline_cache_lock:
            _kline_cache.pop(key, None)
        return None
    return df


def _kline_cache_set(code: str, start: str, end: str, df: pd.DataFrame) -> None:
    from datetime import datetime as _dt
    now = _dt.now()
    # Cache expires at 23:59:59 today (local time)
    expires_at = _dt(now.year, now.month, now.day, 23, 59, 59).timestamp()
    key = _kline_cache_key(code, start, end)
    with _kline_cache_lock:
        _kline_cache[key] = (expires_at, df)


# Symbols that returned "未知股票" are cached here for the process lifetime so
# we don't hammer the API with repeated doomed calls.
_unknown_symbols: set[str] = set()


def _kline_rate_limit_wait() -> None:
    """Block until a kline request slot is available within the rate window."""
    global _kline_last_call_ts
    with _kline_lock:
        now = time.monotonic()

        # 1) 最小间隔：避免连续请求之间间隔过短触发服务端突发限制
        since_last = now - _kline_last_call_ts
        if since_last < _KLINE_MIN_INTERVAL_S:
            time.sleep(_KLINE_MIN_INTERVAL_S - since_last)
            now = time.monotonic()

        # 2) 滑动窗口：驱逐窗口外的旧时间戳
        while _kline_ts and now - _kline_ts[0] >= _KLINE_WINDOW_S:
            _kline_ts.popleft()

        # 3) 窗口配额满时等到最老请求滑出窗口
        if len(_kline_ts) >= _KLINE_MAX_IN_WINDOW:
            sleep_for = _KLINE_WINDOW_S - (now - _kline_ts[0]) + 0.5
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while _kline_ts and now - _kline_ts[0] >= _KLINE_WINDOW_S:
                _kline_ts.popleft()

        _kline_ts.append(time.monotonic())
        _kline_last_call_ts = time.monotonic()


def history_window_for_days(days: int = 600) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def fetch_daily_kline(quote_ctx: Any, code: str, *, days: int = 600) -> pd.DataFrame:
    """
    Fetch daily kline from FutuOpenD.
    Returns DataFrame sorted by time_key ascending.
    """
    from futu import RET_OK

    # Normalize symbol to avoid "未知股票 XXX" caused by whitespace/case issues.
    code = str(code).strip().upper().replace(" ", "")

    # Fast-fail for symbols already confirmed unknown in this session.
    if code in _unknown_symbols:
        raise RuntimeError(
            f"request_history_kline skipped for {code}: 富途 OpenD 在本次运行中已确认该代码无效（未知股票），"
            f"请从监控列表中移除。"
        )

    start, end = history_window_for_days(days)

    # 日内缓存命中 → 直接返回，不消耗任何 API 配额
    cached = _kline_cache_get(code, start, end)
    if cached is not None:
        return cached

    def _normalize_err_msg(err_like: Any) -> str:
        if err_like is None:
            return ""
        if isinstance(err_like, str):
            return err_like.strip()
        return str(err_like).strip()

    def _call_once(page_req_key):
        _kline_rate_limit_wait()
        res = quote_ctx.request_history_kline(
            code=code,
            start=start,
            end=end,
            ktype="K_DAY",
            autype=None,  # no rehab/adjustment; compatible with older futu-api
            max_count=1000,
            page_req_key=page_req_key,
        )

        if not isinstance(res, tuple):
            raise RuntimeError(
                f"Unexpected return from request_history_kline for {code}: type={type(res)} start={start} end={end}"
            )

        # futu-api has historically returned either:
        # - (ret, df, page_req_key_or_errmsg)
        # - (ret, df, page_req_key, err_msg)
        if len(res) == 3:
            ret, df, third = res
            if ret != RET_OK:
                err_msg = _normalize_err_msg(third) or "Unknown error"
                return ret, None, None, err_msg, res
            page_key = None if isinstance(third, str) else third
            return ret, df, page_key, "", res

        if len(res) == 4:
            ret, df, page_key, err_msg = res
            return ret, df, page_key, _normalize_err_msg(err_msg), res

        raise RuntimeError(
            f"Unexpected tuple size from request_history_kline for {code}: size={len(res)} start={start} end={end}"
        )

    _RATE_LIMIT_MSGS = ("频率太高", "too frequent", "too many", "rate limit")

    def _call(page_req_key):
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            ret, df, page_key, err_msg, raw = _call_once(page_req_key)
            if ret == RET_OK:
                return ret, df, page_key, err_msg

            msg_lower = (err_msg or "").lower()

            # Auto-subscribe retry.
            if ("订阅" in err_msg) or ("subscribe" in msg_lower):
                try:
                    from futu import SubType
                    quote_ctx.subscribe([code], [SubType.K_DAY], subscribe_push=False)
                    ret, df, page_key, err_msg, _raw2 = _call_once(page_req_key)
                    if ret == RET_OK:
                        return ret, df, page_key, err_msg
                except Exception:
                    pass

            # Rate-limit retry with back-off.
            is_rate_limited = any(kw in (err_msg or "") for kw in _RATE_LIMIT_MSGS)
            if is_rate_limited and attempt < max_attempts:
                wait_s = [15, 30, 60][attempt - 1]  # 15 s, 30 s, 60 s
                import logging
                logging.warning(
                    "[KLINE] %s 限速，第%d次重试，等待 %ds…", code, attempt, wait_s
                )
                time.sleep(wait_s)
                continue

            break  # non-retryable error

        if not err_msg:
            err_msg = "Unknown error"
        if ("未知股票" in err_msg) or ("unknown stock" in err_msg.lower()):
            _unknown_symbols.add(code)
            err_msg = (
                f"{err_msg}；富途 OpenD 无法识别该代码，已加入本次运行跳过列表。"
                f"请确认代码格式正确（如 US.AAPL），或从监控列表中移除。"
            )
        err_msg = f"ret={ret}; {err_msg} (raw_return={raw!r})"
        return ret, None, None, err_msg

    ret, data, page_key, err = _call(None)
    if ret != RET_OK or data is None:
        raise RuntimeError(f"request_history_kline failed for {code} start={start} end={end}: {err}")

    all_rows = [data]
    while page_key is not None:
        ret, data, page_key, err = _call(page_key)
        if ret != RET_OK or data is None:
            raise RuntimeError(f"request_history_kline paging failed for {code} start={start} end={end}: {err}")
        all_rows.append(data)

    df = pd.concat(all_rows, ignore_index=True)
    if "time_key" in df.columns:
        df = df.sort_values("time_key").reset_index(drop=True)

    # 写入日内缓存，当天剩余所有周期直接命中缓存
    _kline_cache_set(code, start, end, df)
    return df


def get_last_price(quote_ctx: Any, code: str) -> float:
    from futu import RET_OK

    ret, data = quote_ctx.get_stock_quote([code])
    if ret != RET_OK:
        msg = str(data)
        # Auto-subscribe and retry if required by OpenD permissions.
        if "请先订阅" in msg or "subscribe" in msg.lower():
            try:
                from futu import SubType

                quote_ctx.subscribe([code], [SubType.QUOTE], subscribe_push=False)
                ret, data = quote_ctx.get_stock_quote([code])
            except Exception:
                ret = ret
                data = data

        if ret != RET_OK:
            raise RuntimeError(f"get_stock_quote failed for {code}: {data}")
    if data is None or data.empty:
        raise RuntimeError(f"get_stock_quote returned empty for {code}")

    row = data.iloc[0]
    for col in ("last_price", "price"):
        if col in data.columns and pd.notna(row.get(col)):
            return float(row[col])
    raise RuntimeError(f"Cannot find last price for {code}. Columns={list(data.columns)}")

