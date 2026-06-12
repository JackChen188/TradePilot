from __future__ import annotations

import os
import json
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class PushPlusNotifier:
    token_env: str = "PUSHPLUS_TOKEN"
    api_url: str = "http://www.pushplus.plus/send"

    def _token(self) -> str:
        return (os.getenv(self.token_env) or "").strip()

    def _use_wecom(self) -> bool:
        backend = (os.getenv("TP_NOTIFICATION_BACKEND") or "").strip().lower()
        if backend in ("pushplus", "push_plus"):
            return False
        if backend in ("wecom", "wework", "enterprise_wechat"):
            return True
        return True

    def enabled(self) -> bool:
        if self._use_wecom():
            return WeComNotifier().enabled()
        return bool(self._token())

    def send(
        self,
        *,
        title: str,
        content: str,
        timeout: float = 20.0,
        channel: str | None = None,
    ) -> tuple[bool, str]:
        if self._use_wecom():
            return WeComNotifier().send(title=title, content=content, timeout=timeout)

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


@dataclass(frozen=True)
class WeComNotifier:
    webhook_env: str = "TP_WECOM_WEBHOOK_URL"

    def _webhook_url(self) -> str:
        return (
            os.getenv(self.webhook_env)
            or os.getenv("WECOM_WEBHOOK_URL")
            or os.getenv("WEWORK_WEBHOOK_URL")
            or ""
        ).strip()

    def enabled(self) -> bool:
        return bool(self._webhook_url())

    def send(
        self,
        *,
        title: str,
        content: str,
        timeout: float = 20.0,
    ) -> tuple[bool, str]:
        webhook_url = self._webhook_url()
        if not webhook_url:
            return False, f"WeCom webhook missing in env: {self.webhook_env}"

        text = f"{title}\n\n{content}".strip()
        if len(text) > 3900:
            text = text[:3900] + "\n\n[truncated]"

        payload = {
            "msgtype": "text",
            "text": {
                "content": text,
            },
        }

        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            resp = requests.post(
                webhook_url,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=float(timeout),
            )
            text_resp = resp.text or ""
            if not resp.ok:
                return False, text_resp
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("errcode") not in (0, "0", None):
                    return False, text_resp
            except Exception:
                pass
            return True, text_resp
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

