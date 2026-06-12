"""Load optional secrets from logs/secrets.env (works with PyInstaller dist/)."""
from __future__ import annotations

import os
import sys


def resolve_runtime_dir() -> str:
    """
    Directory containing TradePilot.exe or main.py.
    Logs and queue files live under <runtime>/logs/ (do not rely on os.getcwd()).
    """
    override = (os.getenv("TP_RUNTIME_DIR") or "").strip()
    if override and os.path.isdir(override):
        return os.path.abspath(override)
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def resolve_project_root() -> str:
    """
    TradePilot source tree root (clawbot_bridge.mjs, node_modules, config/).
    When exe runs from dist/, parent of dist/ is the project root.
    """
    override = (os.getenv("TP_PROJECT_ROOT") or "").strip()
    if override and os.path.isdir(override):
        return os.path.abspath(override)

    runtime = resolve_runtime_dir()
    if os.path.basename(runtime).lower() == "dist":
        return os.path.dirname(runtime)

    if os.path.isfile(os.path.join(runtime, "clawbot_bridge.mjs")):
        return runtime

    cwd = os.path.abspath(os.getcwd())
    if os.path.basename(cwd).lower() == "dist":
        return os.path.dirname(cwd)
    if os.path.isfile(os.path.join(cwd, "clawbot_bridge.mjs")):
        return cwd
    parent = os.path.dirname(cwd)
    if os.path.isfile(os.path.join(parent, "clawbot_bridge.mjs")):
        return parent

    return runtime


def _project_root() -> str:
    return resolve_project_root()


_CONFIG_OVERRIDE_KEYS = frozenset(
    {
        "TP_PUSHPLUS_CHANNEL",
        "TP_CLAWBOT_REPLY_CHANNEL",
        "TP_NOTIFICATION_BACKEND",
        "TP_WECOM_WEBHOOK_URL",
    }
)


def _parse_dotenv(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                key, _, val = s.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if not key or not val:
                    continue
                if key in _CONFIG_OVERRIDE_KEYS or not (os.getenv(key) or "").strip():
                    os.environ[key] = val
    except Exception:
        pass


def load_secrets_env() -> None:
    """Load secrets.env; TP_PUSHPLUS_CHANNEL in file overrides stale env."""
    candidates = [
        os.path.join(resolve_runtime_dir(), "logs", "secrets.env"),
        os.path.join(_project_root(), "logs", "secrets.env"),
        os.path.join(_project_root(), "secrets.env"),
    ]
    seen: set[str] = set()
    for path in candidates:
        ap = os.path.abspath(path)
        if ap in seen or not os.path.isfile(ap):
            continue
        seen.add(ap)
        _parse_dotenv(ap)


def get_cursor_api_key() -> str:
    load_secrets_env()
    return (os.getenv("CURSOR_API_KEY") or "").strip()


def get_codex_api_key() -> str:
    load_secrets_env()
    return (os.getenv("OPENAI_API_KEY") or "").strip()
