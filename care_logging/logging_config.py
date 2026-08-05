"""Split Care console logging across stdout and stderr by severity.

- DEBUG / INFO / WARNING → stdout (`console` + below-error filter)
- ERROR+ → stderr (`console_error`)
"""

from __future__ import annotations

import copy
import logging
import logging.config
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_apply_lock = threading.Lock()
_applied = False

_STREAM_HANDLER_CLASSES = frozenset(
    {
        "logging.StreamHandler",
        "logging.handlers.StreamHandler",
    }
)


class BelowErrorFilter(logging.Filter):
    """Allow only records strictly below ERROR (for the stdout console handler)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def _is_stream_handler(handler_cfg: dict[str, Any]) -> bool:
    handler_class = handler_cfg.get("class")
    if not isinstance(handler_class, str):
        return False
    if handler_class in _STREAM_HANDLER_CLASSES:
        return True
    # Custom StreamHandler subclasses referenced by dotted path.
    return handler_class.endswith(".StreamHandler") or handler_class.endswith(
        "StreamHandler"
    )


def _formatter_name(handlers: dict[str, Any], fallback: str = "verbose") -> str:
    console = handlers.get("console") or {}
    name = console.get("formatter")
    return name if isinstance(name, str) and name else fallback


def _ensure_formatter(config: dict[str, Any], name: str) -> None:
    formatters = config.setdefault("formatters", {})
    if name in formatters:
        return
    formatters[name] = {
        "format": (
            "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s"
        ),
    }


def _ensure_below_error_filter(config: dict[str, Any]) -> None:
    filters = config.setdefault("filters", {})
    filters["below_error"] = {
        "()": "care_logging.logging_config.BelowErrorFilter",
    }


def _append_filter(handler_cfg: dict[str, Any], filter_name: str) -> None:
    filters = list(handler_cfg.get("filters") or [])
    if filter_name not in filters:
        filters.append(filter_name)
    handler_cfg["filters"] = filters


def _configure_handlers(config: dict[str, Any]) -> None:
    handlers = config.setdefault("handlers", {})
    formatter = _formatter_name(handlers)
    _ensure_formatter(config, formatter)

    console = handlers.get("console")
    if console is None:
        console = {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": formatter,
        }
        handlers["console"] = console

    if _is_stream_handler(console):
        console["stream"] = "ext://sys.stdout"
        _append_filter(console, "below_error")
    else:
        # Do not mutate non-stream handlers (e.g. JSON / file handlers).
        # Add a dedicated stdout handler for below-ERROR traffic instead.
        handlers["care_logging_stdout"] = {
            "level": console.get("level", "DEBUG"),
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": formatter,
            "filters": ["below_error"],
        }

    handlers["console_error"] = {
        "level": "ERROR",
        "class": "logging.StreamHandler",
        "stream": "ext://sys.stderr",
        "formatter": formatter,
    }

    time_logging = handlers.get("time_logging")
    if isinstance(time_logging, dict) and _is_stream_handler(time_logging):
        time_logging["stream"] = "ext://sys.stdout"
        _append_filter(time_logging, "below_error")


def _configure_root(config: dict[str, Any]) -> None:
    root = config.setdefault("root", {"level": "INFO", "handlers": ["console"]})
    root_handlers = list(root.get("handlers") or [])

    handlers = config.get("handlers") or {}
    stdout_handler = (
        "console"
        if "console" in handlers and _is_stream_handler(handlers["console"])
        else "care_logging_stdout"
        if "care_logging_stdout" in handlers
        else "console"
    )

    if stdout_handler not in root_handlers:
        root_handlers.insert(0, stdout_handler)
    if "console_error" not in root_handlers:
        root_handlers.append("console_error")

    # If we introduced care_logging_stdout because console was non-stream,
    # keep the original console handler as well so existing sinks remain.
    root["handlers"] = root_handlers


def _reroute_error_loggers(config: dict[str, Any]) -> None:
    """Point ERROR-only loggers at console_error and disable propagate.

    Avoids duplicate stderr lines when root also has console_error.
    """
    loggers = config.setdefault("loggers", {})
    for name, logger_cfg in list(loggers.items()):
        if not isinstance(logger_cfg, dict):
            continue
        level = str(logger_cfg.get("level") or "").upper()
        if level != "ERROR":
            continue
        handlers = list(logger_cfg.get("handlers") or [])
        if not handlers:
            continue
        logger_cfg["handlers"] = [
            "console_error" if handler == "console" else handler for handler in handlers
        ]
        logger_cfg["propagate"] = False
        loggers[name] = logger_cfg


def build_split_logging_config(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a LOGGING dict with the stdout/stderr split applied."""
    source = base if base is not None else getattr(settings, "LOGGING", None)
    try:
        config = copy.deepcopy(source) if source else {}
    except Exception:
        # Fall back if LOGGING contains un-copyable objects.
        logger.warning(
            "care_logging: could not deepcopy settings.LOGGING; starting from empty config",
            exc_info=True,
        )
        config = {}

    if not config:
        config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {},
            "handlers": {},
            "root": {"level": "INFO", "handlers": ["console"]},
        }

    config["version"] = config.get("version", 1)
    # Never disable existing loggers — preserve Care / third-party loggers.
    config["disable_existing_loggers"] = False

    _ensure_below_error_filter(config)
    _configure_handlers(config)
    _configure_root(config)
    _reroute_error_loggers(config)
    return config


def apply_split_console_logging(*, force: bool = False) -> bool:
    """Reconfigure process logging from Care's LOGGING settings.

    Returns True when configuration was applied. Failures are logged and do not
    raise, so a bad transform cannot take down Care startup.
    """
    global _applied

    with _apply_lock:
        if _applied and not force:
            return False
        try:
            config = build_split_logging_config()
            logging.config.dictConfig(config)
            _applied = True
            logger.info("care_logging: applied stdout/stderr console split")
            return True
        except Exception:
            logger.exception(
                "care_logging: failed to apply split console logging; "
                "leaving the existing logging configuration in place"
            )
            return False


def reset_apply_state() -> None:
    """Test helper to clear the idempotency guard."""
    global _applied
    with _apply_lock:
        _applied = False
