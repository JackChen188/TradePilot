"""Load optional secrets from logs/secrets.env (works with PyInstaller dist/)."""
from __future__ import annotations

import os


def _project_root() -> str:
    cwd = os.path.abspath(os.getcwd())
    if os.path.basename(cwd).lower() == "dist":
        return os.path.dirname(cwd)
    return cwd


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
                if key and val and not (os.getenv(key) or "").strip():
                    os.environ[key] = val
    except Exception:
        pass


def load_secrets_env() -> None:
    """Load secrets.env without overriding existing environment variables."""
    candidates = [
        os.path.join(os.getcwd(), "logs", "secrets.env"),
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
