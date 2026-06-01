"""Quick ClawBot / PushPlus connectivity check. Run: py scripts/check_clawbot.py"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.argv[0] = r"dist\TradePilot.exe"

from secrets_loader import load_secrets_env

load_secrets_env()

from pushplus_confirm import _access_key, _fetch_clawbot_messages, _pushplus_api_bases


def main() -> int:
    print("API bases:", _pushplus_api_bases())
    ak = _access_key()
    print("accessKey:", (ak[:8] + "...") if ak else "MISSING")
    if not ak:
        print("FAIL: 需要 PUSHPLUS_TOKEN + PUSHPLUS_SECRET_KEY in dist/logs/secrets.env")
        return 1
    msgs = _fetch_clawbot_messages(long_poll=False)
    if msgs is None:
        print("FAIL: getMsg 不可用（DNS/网络/防火墙）")
        return 2
    print("OK: getMsg 可用，当前队列消息数:", len(msgs))
    for m in msgs[:3]:
        print(" -", (m.get("text") or "")[:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
