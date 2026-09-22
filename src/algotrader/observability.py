"""
Logging / observability.

- One `configure_logging()` call at process start; modules just use
  `logging.getLogger(__name__)`.
- Console (human) or JSON-lines (machines) format.
- A redaction filter scrubs every configured secret from every record, so a
  stray f-string or exception message can't leak credentials into log files.
- Structured context via `extra={"ctx": {...}}` is emitted as JSON fields.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

_REDACTED = "***REDACTED***"


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Longest first so a secret that contains another is fully replaced.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self._scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            # Render the traceback now so it can be scrubbed too.
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._scrub(formatted)
            record.exc_info = None
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            record.ctx = {k: self._scrub(v) if isinstance(v, str) else v for k, v in ctx.items()}
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool) -> None:
        super().__init__(fmt="%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s", datefmt="%H:%M:%S")
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict) and ctx:
            text += " | " + " ".join(f"{k}={v}" for k, v in ctx.items())
        if self._color:
            color = self.COLORS.get(record.levelno, "")
            text = f"{color}{text}{self.RESET}"
        return text


def configure_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_dir: Path | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Idempotently configure the root logger."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)

    redactor = RedactingFilter(secrets)
    # Windows consoles default to a legacy code page; never let a log line crash on encoding.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    console = logging.StreamHandler(sys.stdout)
    console.addFilter(redactor)
    console.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter(color=sys.stdout.isatty()))
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / "algotrader.jsonl", when="midnight", backupCount=30, encoding="utf-8"
        )
        file_handler.addFilter(redactor)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # Third-party loggers are noisy at DEBUG and some (SmartApi) log request payloads.
    for noisy in ("SmartApi", "smartConnect", "websocket", "httpx", "httpcore", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
