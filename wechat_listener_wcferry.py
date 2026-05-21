from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from config import TRADE


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _msg_hash(sender: str, chat: str, text: str) -> str:
    raw = f"{sender}|{chat}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()


def _append_inbox(sender: str, chat: str, text: str) -> None:
    os.makedirs(os.path.dirname(TRADE.wechat_inbox_path), exist_ok=True)
    record = {"ts_utc": _now_iso(), "sender": sender, "chat": chat, "text": text, "source": "wcferry"}
    with open(TRADE.wechat_inbox_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_seen(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        return set(raw.get("seen_hashes", []))
    except Exception:
        return set()


def _save_seen(path: str, seen: set[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen_hashes": list(seen)[-2000:]}, f, ensure_ascii=False, indent=2)


def main() -> None:
    # wcferry may require admin privileges to read WeChat install path from registry.
    from wcferry import Wcf

    # Preflight: wcferry initialization commonly fails (or hangs) if WeChatFerry's expected exe names
    # are missing in InstallPath. We check that early to give actionable errors.
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat", 0, winreg.KEY_READ
        ) as k:
            install_path, _ = winreg.QueryValueEx(k, "InstallPath")
            install_path = str(install_path)
    except Exception:
        install_path = ""

    if install_path:
        need_exes = ["WeChat.exe", "WeChatAppEx.exe"]
        missing = [exe for exe in need_exes if not os.path.exists(os.path.join(install_path, exe))]
        if missing:
            print("[wcferry_listener] Preflight check failed.")
            print(f"[wcferry_listener] registry InstallPath={install_path}")
            print(f"[wcferry_listener] missing: {', '.join(missing)}")
            print("[wcferry_listener] Fix (run in Administrator terminal):")
            print(f"  cd \"{install_path}\"")
            print("  if not exist WeChat.exe mklink WeChat.exe Weixin.exe")
            print("  if not exist WeChatAppEx.exe mklink WeChatAppEx.exe Weixin.exe")
            print("  (If mklink fails, use copy instead: copy /Y Weixin.exe WeChat.exe)")
            raise SystemExit(1)

    seen_path = os.path.join("logs", "wechat_listener_wcferry_seen.json")
    seen = _load_seen(seen_path)

    try:
        wcf = Wcf(debug=False, block=True)
    except Exception as e:
        msg = str(e).lower()
        # If the SDK was blocked/quarantined by Windows Defender, the file may disappear.
        if isinstance(e, FileNotFoundError) and "sdk.dll" in msg:
            print("[wcferry_listener] sdk.dll is missing.")
            print("[wcferry_listener] This usually means Windows Defender quarantined/blocked it.")
            print("[wcferry_listener] Fix: add an exclusion for this folder in Windows Security,")
            print(f"  C:\\Users\\JieChen\\AppData\\Local\\Programs\\Python\\Python39\\lib\\site-packages\\wcferry")
            print("Then reinstall wcferry and retry the listener.")
            raise

        # Windows Defender/安全软件有时会拦截注入类 SDK，
        # 典型报错是 WinError 225（Operation did not complete successfully...virus/PUA）。
        winerr = getattr(e, "winerror", None)
        if isinstance(e, OSError) and winerr == 225:
            print("[wcferry_listener] WeChatFerry init blocked by security software (WinError=225).")
            print("[wcferry_listener] 请在 Windows 安全中心对以下内容做排除/允许：")
            print(f"  {os.path.join(os.path.dirname(__file__), 'sdk.dll')}")
            print(f"  {os.path.dirname(__file__)} (wcferry 目录)")
            print("[wcferry_listener] 操作后请重启监听脚本。")
            raise

        # Common failure: wcferry can't read WeChat install path from Windows registry.
        weixin_dirs = [
            r"C:\Program Files (x86)\Tencent\Weixin",
            r"C:\Program Files\Tencent\Weixin",
        ]
        detected_weixin_dir = ""
        for d in weixin_dirs:
            if os.path.exists(os.path.join(d, "Weixin.exe")):
                detected_weixin_dir = d
                break

        print("[wcferry_listener] WeChatFerry init failed.")
        print(f"[wcferry_listener] error={type(e).__name__}: {e}")
        if detected_weixin_dir:
            print(f"[wcferry_listener] Detected: {detected_weixin_dir}\\Weixin.exe")
        print("[wcferry_listener] If your install is Weixin.exe (not WeChat.exe),")
        print("                please ensure registry has InstallPath and create WeChat.exe aliases if needed.")
        print("[wcferry_listener] Example (run in Admin terminal):")
        print("  reg add \"HKCU\\Software\\Tencent\\WeChat\" /v InstallPath /t REG_SZ /d \"C:\\Program Files (x86)\\Tencent\\Weixin\" /f")
        print("  cd \"C:\\Program Files (x86)\\Tencent\\Weixin\"")
        print("  if not exist WeChat.exe mklink WeChat.exe Weixin.exe")
        print("  if not exist WeChatAppEx.exe mklink WeChatAppEx.exe Weixin.exe")
        raise
    if not wcf.is_login():
        raise SystemExit("wcferry: not logged in. Please log in on WeChat first.")

    allowed_chats = [c.strip() for c in TRADE.wechat_allowed_chats if str(c).strip()]
    allowed_chat_set = set(allowed_chats)
    if not allowed_chats:
        raise SystemExit("No allowed chats configured (WECHAT_ALLOWED_CHATS/TRADE.wechat_allowed_chats empty).")

    friends = wcf.get_friends() or []
    # Build mapping chat_name -> wxid for allowed chats
    chat_to_wxid: dict[str, str] = {}
    for f in friends:
        name = str(f.get("name") or "").strip()
        if not name or name not in allowed_chat_set:
            continue
        wxid = str(f.get("wxid") or f.get("id") or "").strip()
        if wxid:
            chat_to_wxid[name] = wxid

    # Invert: wxid -> chat_name, so we can label incoming direct messages.
    wxid_to_chat: dict[str, str] = {wxid: name for name, wxid in chat_to_wxid.items()}

    # Default: if config uses "文件传输助手", map it
    filehelper_wxid = chat_to_wxid.get("文件传输助手") or chat_to_wxid.get("File Transfer Assistant") or ""
    if not filehelper_wxid and allowed_chats:
        # Fallback: try exact match by suffix
        for ch in allowed_chats:
            if ch and ch in chat_to_wxid:
                filehelper_wxid = chat_to_wxid[ch]
                break

    print(f"[wcferry_listener] allowed_chats={allowed_chats} filehelper_wxid={filehelper_wxid}")

    # Start receiving.
    ok = wcf.enable_receiving_msg()
    if not ok:
        raise SystemExit("wcferry: enable_receiving_msg failed.")

    while True:
        try:
            msg = wcf.get_msg(block=True)
            if msg is None:
                time.sleep(0.1)
                continue
            # We only use text messages.
            if not getattr(msg, "is_text", lambda: True)():
                continue
            sender = str(msg.sender or "").strip()
            text = str(msg.content or "").strip()
            if not sender or not text:
                continue
            # Determine the chat by sender wxid.
            # This allows using a dedicated "bot chat" instead of listening to all messages.
            chat = wxid_to_chat.get(sender, "")
            if not chat and filehelper_wxid and sender == filehelper_wxid:
                chat = "文件传输助手"
            if not chat or chat not in allowed_chat_set:
                # Still mark seen to avoid reprocessing, but do not write to inbox.
                h = _msg_hash(sender, chat or "unknown", text)
                if h in seen:
                    continue
                seen.add(h)
                if len(seen) > 4000:
                    seen = set(list(seen)[-2000:])
                _save_seen(seen_path, seen)
                continue

            h = _msg_hash(sender, chat, text)
            if h in seen:
                continue
            seen.add(h)
            _append_inbox(sender=sender, chat=chat, text=text)
            if len(seen) > 4000:
                seen = set(list(seen)[-2000:])
            _save_seen(seen_path, seen)
        except KeyboardInterrupt:
            break
        except Exception as e:
            # Don't crash the listener; keep running.
            print(f"[wcferry_listener] error: {type(e).__name__}: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()

