from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from config import TRADE


def _now_iso() -> str:
    # utc for consistency
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg_hash(sender: str, chat: str, text: str) -> str:
    raw = f"{sender}|{chat}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()


def _append_inbox_record(*, sender: str, chat: str, text: str) -> None:
    os.makedirs(os.path.dirname(TRADE.wechat_inbox_path), exist_ok=True)
    record = {"ts_utc": _now_iso(), "sender": sender, "chat": chat, "text": text, "source": "wx_listener"}
    with open(TRADE.wechat_inbox_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _get_attr(obj: Any, *names: str, default: str = "") -> str:
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None:
                return str(v).strip()
        except Exception:
            continue
    return default


def main() -> None:
    # Lazy import to make error clearer.
    try:
        from wxauto4 import WeChat  # type: ignore
    except Exception as e:
        raise SystemExit(f"wxauto4 import failed: {e}. Install wxauto4 first.")

    allowed_chats = [c for c in TRADE.wechat_allowed_chats if str(c).strip()]
    if not allowed_chats:
        raise SystemExit("No allowed chats configured (WECHAT_ALLOWED_CHATS/TRADE.wechat_allowed_chats empty).")

    # Retry until WeChat main window is detected.
    wx = None
    last_err = None
    for _ in range(60):  # ~5 minutes
        try:
            # wxauto4 may fail to locate UI controls depending on WeChat language/window state.
            # Try a few language settings before giving up this round.
            init_candidates = [
                {"ads": False},
                {"ads": False, "language": "cn_t"},
                {"ads": False, "language": "cn"},
                {"ads": False, "language": "en"},
                {"ads": False, "LANGUAGE": "cn_t"},
                {"ads": False, "LANGUAGE": "en"},
            ]
            last_local_err = None
            for kw in init_candidates:
                try:
                    wx = WeChat(**kw)  # type: ignore[arg-type]
                    last_local_err = None
                    break
                except TypeError:
                    # Some builds don't accept 'language'/'LANGUAGE' keyword.
                    continue
                except Exception as e:
                    last_local_err = e
                    continue
            if wx is not None:
                break
            last_err = last_local_err
        except Exception as e:
            last_err = e
            msg = str(e)
            if "未找到已登录的客户端主窗口" in msg or "主窗口" in msg:
                print("[wechat_listener] Waiting for WeChat main window... (open WeChat and do not close/minimize)", flush=True)
            else:
                # repr(e) avoids encoding garbage from wxauto4 internals.
                print(f"[wechat_listener] WeChat init failed: {type(e).__name__}: {repr(e)}", flush=True)
            time.sleep(5)

    if wx is None:
        # Use repr() to avoid garbled output from non-UTF8 exceptions.
        raise SystemExit(
            "wxauto4 init failed after retry. "
            f"last_err_type={type(last_err).__name__} last_err_repr={repr(last_err)}"
        )

    if not hasattr(wx, "GetListenMessage"):
        raise SystemExit("wxauto4 instance has no GetListenMessage. Cannot poll messages.")

    # Best-effort register listeners (some builds omit AddListenChat but still support GetListenMessage).
    if hasattr(wx, "AddListenChat"):
        for chat in allowed_chats:
            try:
                wx.AddListenChat(chat)
            except Exception:
                pass
    elif hasattr(wx, "AddListen"):
        for chat in allowed_chats:
            try:
                wx.AddListen(chat)
            except Exception:
                pass

    state_path = os.path.join("logs", "wechat_listener_state.json")
    processed: set[str] = set()
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            processed = set(raw.get("processed_hashes", []))
        except Exception:
            processed = set()

    print(f"Listening chats={allowed_chats}")
    while True:
        try:
            msgs = wx.GetListenMessage()
            # msgs may be dict(chat_name -> list[msg])
            if isinstance(msgs, dict):
                for chat_name, msg_list in msgs.items():
                    chat = str(chat_name).strip()
                    if chat not in allowed_chats:
                        continue
                    for m in msg_list or []:
                        text = _get_attr(m, "content", "text", default="")
                        if not text:
                            continue
                        # Sender might be nickname or wxid depending on wxauto4 build.
                        sender = _get_attr(m, "sender", "from_user", "from", "NickName", "wxid", default="unknown")
                        h = _msg_hash(sender, chat, text)
                        if h in processed:
                            continue
                        processed.add(h)
                        _append_inbox_record(sender=sender, chat=chat, text=text)
            # Keep state from growing unbounded
            if len(processed) > 4000:
                processed = set(list(processed)[-2000:])

            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"processed_hashes": list(processed)}, f, ensure_ascii=False, indent=2)

        except KeyboardInterrupt:
            break
        except Exception as e:
            # Do not crash; just continue and wait next poll.
            print(f"[wechat_listener] error: {type(e).__name__}: {e}")

        time.sleep(0.5)


if __name__ == "__main__":
    main()

