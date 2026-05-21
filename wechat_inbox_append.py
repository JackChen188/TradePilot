from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from config import TRADE


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: py wechat_inbox_append.py <sender> <chat> <text>")
        print('Example: py wechat_inbox_append.py 15927132988 文件传输助手 "YES BUY US.HUT 2 ABC123"')
        raise SystemExit(2)

    sender = str(sys.argv[1]).strip()
    chat = str(sys.argv[2]).strip()
    text = " ".join(sys.argv[3:]).strip()
    if not sender or not chat or not text:
        print("sender/chat/text cannot be empty")
        raise SystemExit(2)

    record = {
        "ts_utc": _now_iso(),
        "sender": sender,
        "chat": chat,
        "text": text,
        "source": "manual_append",
    }

    path = TRADE.wechat_inbox_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Appended to {path}")
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()

