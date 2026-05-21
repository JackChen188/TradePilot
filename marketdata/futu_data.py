from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd


def default_history_window() -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=500)
    return (start.isoformat(), end.isoformat())


def fetch_history_kline(
    quote_ctx: Any,
    code: str,
    *,
    start: str,
    end: str,
    ktype: Any,
    rehab: Any,
) -> pd.DataFrame:
    from futu import RET_OK

    def _call(page_req_key):
        res = quote_ctx.request_history_kline(
            code=code,
            start=start,
            end=end,
            ktype=ktype,
            rehab_type=rehab,
            max_count=1000,
            page_req_key=page_req_key,
        )

        # Normalize to (ret, df, page_key, err_msg)
        if not isinstance(res, tuple):
            raise RuntimeError(f"Unexpected return from request_history_kline: {type(res)}")

        if len(res) == 3:
            ret, df, third = res
            if ret != RET_OK:
                err_msg = third if isinstance(third, str) else "Unknown error"
                return ret, None, None, err_msg
            page_key = None if isinstance(third, str) else third
            return ret, df, page_key, ""

        if len(res) == 4:
            ret, df, page_key, err_msg = res
            return ret, df, page_key, err_msg

        raise RuntimeError(f"Unexpected tuple size from request_history_kline: {len(res)}")

    ret, data, page_key, err = _call(None)
    if ret != RET_OK or data is None:
        raise RuntimeError(f"request_history_kline failed for {code}: {err}")

    all_rows = [data]
    while page_key is not None:
        ret, data, page_key, err = _call(page_key)
        if ret != RET_OK or data is None:
            raise RuntimeError(f"request_history_kline paging failed for {code}: {err}")
        all_rows.append(data)

    df = pd.concat(all_rows, ignore_index=True)
    if "time_key" in df.columns:
        df = df.sort_values("time_key").reset_index(drop=True)
    return df


def get_last_price(quote_ctx: Any, code: str) -> float:
    from futu import RET_OK

    ret, data = quote_ctx.get_stock_quote([code])
    if ret != RET_OK:
        raise RuntimeError(f"get_stock_quote failed for {code}: {data}")
    if data is None or data.empty:
        raise RuntimeError(f"get_stock_quote returned empty for {code}")

    # Prefer last_price; fallback to price
    row = data.iloc[0]
    for col in ("last_price", "price"):
        if col in data.columns and pd.notna(row.get(col)):
            return float(row[col])
    raise RuntimeError(f"Cannot find last price column for {code}. Columns={list(data.columns)}")

