from __future__ import annotations

import os
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class PushPlusNotifier:
    token_env: str = "PUSHPLUS_TOKEN"
    api_url: str = "http://www.pushplus.plus/send"

    def _token(self) -> str:
        return (os.getenv(self.token_env) or "").strip()

    def enabled(self) -> bool:
        return bool(self._token())

    def send(self, *, title: str, content: str, timeout: float = 20.0) -> tuple[bool, str]:
        token = self._token()
        if not token:
            return False, f"PushPlus token missing in env: {self.token_env}"

        # 渠道优先级：环境变量 TP_PUSHPLUS_CHANNEL（默认空，走 PushPlus 默认渠道）
        # 设置为 "clawbot" 时消息将通过微信 ClawBot 推送到微信
        channel = (os.getenv("TP_PUSHPLUS_CHANNEL") or "").strip().lower()

        payload: dict = {
            "token": token,
            "title": title,
            "content": content,
            "template": "txt",  # ClawBot 只支持纯文本
        }
        if channel:
            payload["channel"] = channel

        try:
            resp = requests.post(self.api_url, json=payload, timeout=float(timeout))
            return resp.ok, resp.text
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

