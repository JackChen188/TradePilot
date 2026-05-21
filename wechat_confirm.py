from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config import TRADE
from data_provider import get_last_price
from holdings import apply_buy, apply_sell, load_holdings, save_holdings
from notifier import PushPlusNotifier
from pending_orders import PendingOrder, expire_pending_orders, load_pending_orders, save_pending_orders
from report import append_trade_record
from risk_manager import append_trade_log

SECURITY_LOG_PATH = os.path.join("logs", "security_log.csv")
INBOX_STATE_PATH = os.path.join("logs", "wechat_inbox_state.json")

CMD_RE = re.compile(r"^YES\s+(BUY|SELL)\s+([A-Z]{2}\.[A-Z0-9]+)\s+(\d+)\s+([A-Z0-9]{4,16})$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _extract_order_id(data: Any) -> str | None:
    if isinstance(data, pd.DataFrame) and not data.empty and "order_id" in data.columns:
        try:
            return str(data.iloc[0]["order_id"])
        except Exception:
            return None
    return None


def _security_log(*, sender: str, chat: str, text: str, result: str, reason: str) -> None:
    os.makedirs(os.path.dirname(SECURITY_LOG_PATH), exist_ok=True)
    fields = ["ts_utc", "sender", "chat", "text", "result", "reason"]
    exists = os.path.exists(SECURITY_LOG_PATH) and os.path.getsize(SECURITY_LOG_PATH) > 0
    with open(SECURITY_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(
            {
                "ts_utc": _now_iso(),
                "sender": sender,
                "chat": chat,
                "text": text,
                "result": result,
                "reason": reason,
            }
        )


def _load_state() -> dict:
    os.makedirs(os.path.dirname(INBOX_STATE_PATH), exist_ok=True)
    if not os.path.exists(INBOX_STATE_PATH):
        return {"processed_ids": []}
    try:
        with open(INBOX_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"processed_ids": []}
    except Exception:
        return {"processed_ids": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(INBOX_STATE_PATH), exist_ok=True)
    with open(INBOX_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _msg_id(msg: dict) -> str:
    raw = json.dumps(msg, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _iter_inbox_messages(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                out.append(json.loads(s))
            except Exception:
                continue
    return out


def _notify_fail(notifier: PushPlusNotifier, *, sender: str, reason: str, text: str) -> None:
    notifier.send(
        title="TradePilot 微信确认失败",
        content=f"sender={sender}\nreason={reason}\ncommand={text}",
    )


def _execute_pending(
    *,
    po: PendingOrder,
    broker,
    notifier: PushPlusNotifier,
    sender: str,
    chat: str,
) -> None:
    side = po.side.upper()
    current_price = get_last_price(broker.quote_ctx, po.symbol)
    est_amount = float(po.qty) * float(po.limit_price)
    if est_amount > float(TRADE.max_order_usd):
        raise RuntimeError(f"Blocked: single order amount ${est_amount:.2f} > max ${TRADE.max_order_usd:.2f}")
    if side == "BUY":
        available_cash = broker.get_available_cash()
        if available_cash < est_amount:
            raise RuntimeError(f"Blocked: insufficient cash for BUY. cash={available_cash:.2f} need={est_amount:.2f}")
    else:
        held_qty = broker.get_position_qty(po.symbol)
        if held_qty < int(po.qty):
            raise RuntimeError(f"Blocked: insufficient position for SELL. held={held_qty} need={po.qty}")

    broker.ensure_us_stock_only(po.symbol)
    broker.ensure_regular_session(po.symbol)

    ok, data = broker.place_limit_order(
        code=po.symbol,
        side=side,
        qty=int(po.qty),
        price=float(po.limit_price),
    )
    order_id = _extract_order_id(data) or ""
    status_msg = ""
    if order_id:
        try:
            qdf = broker.query_order(order_id)
            if qdf is not None and not qdf.empty:
                status_msg = str(qdf.iloc[0].get("order_status", ""))
        except Exception:
            status_msg = ""

    po.broker_order_id = order_id
    po.broker_status = status_msg
    po.updated_at = _now_iso()
    po.confirm_code = ""  # invalidate immediately
    # Align with main.py's internal status naming.
    po.status = "EXECUTED" if ok else "FAILED"
    po.message = str(data)

    append_trade_log(
        {
            "code": po.symbol,
            "selected": 1,
            "action": side,
            "current_price": current_price,
            "limit_price": po.limit_price,
            "qty": po.qty,
            "est_amount": est_amount,
            "order_ok": 1 if ok else 0,
            "order_id": order_id,
            "order_status": status_msg,
            "message": f"wechat sender={sender} chat={chat}; {data}",
        }
    )

    if ok:
        holdings = load_holdings()
        if side == "BUY":
            apply_buy(holdings, symbol=po.symbol, qty=int(po.qty), price=float(po.limit_price))
        else:
            apply_sell(holdings, symbol=po.symbol, qty=int(po.qty))
        save_holdings(holdings)
        append_trade_record(
            {
                "ts_utc": _now_iso(),
                "symbol": po.symbol,
                "side": side,
                "qty": int(po.qty),
                "price": float(po.limit_price),
                "order_id": order_id,
            }
        )

    notifier.send(
        title=f"TradePilot 微信下单结果 {side} {po.symbol}",
        content=(
            f"sender={sender}\nchat={chat}\n"
            f"symbol={po.symbol}\nqty={po.qty}\nprice={po.limit_price}\n"
            f"order_ok={ok}\norder_id={order_id}\nstatus={status_msg}\nmessage={data}"
        ),
    )


def process_wechat_confirmations(*, broker, notifier: PushPlusNotifier) -> None:
    msgs = _iter_inbox_messages(TRADE.wechat_inbox_path)
    if not msgs:
        return
    state = _load_state()
    processed = set(state.get("processed_ids", []))

    pending_orders = load_pending_orders()
    changed = False
    if expire_pending_orders(pending_orders):
        changed = True

    allowed_senders = set([x.strip() for x in TRADE.wechat_allowed_senders if str(x).strip()])
    allowed_chats = set([x.strip() for x in TRADE.wechat_allowed_chats if str(x).strip()])

    for msg in msgs:
        mid = _msg_id(msg)
        if mid in processed:
            continue

        sender = str(msg.get("sender", "")).strip()
        chat = str(msg.get("chat", "")).strip()
        text = str(msg.get("text", "")).strip()

        # Only monitor specified chat windows
        if chat not in allowed_chats:
            _security_log(sender=sender, chat=chat, text=text, result="IGNORED", reason="chat_not_allowed")
            processed.add(mid)
            continue
        # Sender whitelist
        if sender not in allowed_senders:
            _security_log(sender=sender, chat=chat, text=text, result="REJECTED", reason="sender_not_whitelisted")
            _notify_fail(notifier, sender=sender, reason="sender_not_whitelisted", text=text)
            processed.add(mid)
            continue

        m = CMD_RE.match(text.upper())
        if not m:
            _security_log(sender=sender, chat=chat, text=text, result="REJECTED", reason="invalid_command_format")
            _notify_fail(notifier, sender=sender, reason="invalid_command_format", text=text)
            processed.add(mid)
            continue

        side, symbol, qty_s, confirm_code = m.groups()
        qty = int(qty_s)

        # Full pending validation
        match_po = None
        now = _utc_now()
        for po in pending_orders:
            if po.status != "PENDING":
                continue
            if _parse_iso(po.expire_time) < now:
                continue
            if (
                po.side.upper() == side.upper()
                and po.symbol.upper() == symbol.upper()
                and int(po.qty) == int(qty)
                and str(po.confirm_code).upper() == confirm_code.upper()
            ):
                match_po = po
                break

        if match_po is None:
            _security_log(sender=sender, chat=chat, text=text, result="REJECTED", reason="pending_validation_failed")
            _notify_fail(notifier, sender=sender, reason="pending_validation_failed", text=text)
            processed.add(mid)
            continue

        try:
            _execute_pending(po=match_po, broker=broker, notifier=notifier, sender=sender, chat=chat)
            changed = True
            _security_log(sender=sender, chat=chat, text=text, result="ACCEPTED", reason="order_executed")
        except Exception as e:
            _security_log(sender=sender, chat=chat, text=text, result="REJECTED", reason=f"execute_failed:{type(e).__name__}:{e}")
            _notify_fail(notifier, sender=sender, reason=f"execute_failed:{e}", text=text)
        finally:
            processed.add(mid)

    if changed:
        save_pending_orders(pending_orders)

    # Keep last 2000 processed IDs
    state["processed_ids"] = list(processed)[-2000:]
    _save_state(state)

