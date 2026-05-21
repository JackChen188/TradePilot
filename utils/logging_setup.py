from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(*, log_dir: str = "logs", log_file: str = "tradepilot.log") -> None:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, log_file)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Avoid duplicated handlers on re-run
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return

    file_handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    root.addHandler(file_handler)
    root.addHandler(console)

