from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from .settings import Settings, settings

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def get_component_logger(component: str, runtime_settings: Settings | None = None) -> logging.Logger:
    resolved_settings = runtime_settings or settings
    safe_component = re.sub(r"[^A-Za-z0-9_.-]+", "_", component).strip("._") or "app"
    resolved_settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = resolved_settings.log_dir / f"{safe_component}.log"

    logger = logging.getLogger(f"porto_chatbot.{safe_component}.{hash(str(log_file))}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not any(
        isinstance(handler, RotatingFileHandler)
        and getattr(handler, "baseFilename", None) == str(log_file)
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            log_file,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    return logger
