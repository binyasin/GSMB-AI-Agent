"""Structured logging setup.

Produces logs/application.log (everything), logs/calls.log (the "calls"
logger only), logs/scheduler.log (the "scheduler" logger only), and
logs/errors.log (ERROR+ from anywhere). Known secret values (Twilio auth
token, AI API key, Google service-account JSON) are redacted from every
record before it's written.
"""

from __future__ import annotations

import json
import logging
import logging.config
from pathlib import Path

from app.config import get_settings

_REDACTED = "***REDACTED***"


class SecretMaskingFilter(logging.Filter):
    """Replaces any occurrence of a known secret value with a placeholder."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets = self._collect_secrets()

    @staticmethod
    def _collect_secrets() -> list[str]:
        settings = get_settings()
        candidates = [
            settings.twilio_auth_token,
            settings.ai_api_key,
            settings.control_api_token,
            settings.google_service_account_json,
            settings.google_speech_credentials_json,
        ]
        return [c for c in candidates if c and len(c) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, _REDACTED)
        record.msg = msg
        record.args = ()
        return True


def _handler(path: Path, level: int, fmt: logging.Formatter) -> logging.Handler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(fmt)
    handler.addFilter(SecretMaskingFilter())
    return handler


def setup_logging(log_dir: str | Path = "logs") -> None:
    settings = get_settings()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Structured fields are passed as JSON within the message itself (see
    # log_json below) rather than via a custom Formatter field, since that
    # works uniformly across the console and every file handler.
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    root.handlers.clear()

    root.addHandler(_handler(log_dir / "application.log", logging.DEBUG, fmt))
    root.addHandler(_handler(log_dir / "errors.log", logging.ERROR, fmt))

    console = logging.StreamHandler()
    console.setLevel(settings.log_level.upper())
    console.setFormatter(fmt)
    console.addFilter(SecretMaskingFilter())
    root.addHandler(console)

    calls_logger = logging.getLogger("calls")
    calls_logger.addHandler(_handler(log_dir / "calls.log", logging.DEBUG, fmt))
    calls_logger.propagate = True

    scheduler_logger = logging.getLogger("scheduler")
    scheduler_logger.addHandler(_handler(log_dir / "scheduler.log", logging.DEBUG, fmt))
    scheduler_logger.propagate = True


def log_json(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """Emit a structured line: `event` plus arbitrary JSON-serializable fields."""
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, default=str))
