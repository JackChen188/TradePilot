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

    def send(
        self,
        *,
        title: str,
        content: str,
        timeout: float = 20.0,
        channel: str | None = None,
    ) -> tuple[bool, str]:
        token = self._token()
        if not token:
            return False, f"PushPlus token missing in env: {self.token_env}"

        # channel 参数优先；否则 TP_PUSHPLUS_CHANNEL（支持逗号多通道，如 clawbot,wechat）
        channel = (channel or os.getenv("TP_PUSHPLUS_CHANNEL") or "").strip()
        if channel:
            channel = ",".join(c.strip().lower() for c in channel.split(",") if c.strip())

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
            text = resp.text or ""
            if not resp.ok:
                return False, text
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("code") not in (200, "200"):
                    return False, text
            except Exception:
                pass
            return True, text
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

