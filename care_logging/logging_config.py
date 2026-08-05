"""Split Care console logging across stdout and stderr by severity.

- DEBUG / INFO / WARNING → stdout
- ERROR / CRITICAL → stderr

Applied for Django/Gunicorn via ``dictConfig`` from Care's ``LOGGING`` settings,
and re-applied for Celery worker/beat via ``after_setup_*`` signals after Celery
hijacks the root logger.
"""

from __future__ import annotations

import copy
import logging
import logging.config
import sys
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_apply_lock = threading.Lock()
_celery_hooks_installed = False

# Plug-specific dictConfig names — never overwrite Care's generic keys.
FILTER_NAME = "care_logging_below_error"
STDOUT_HANDLER_NAME = "care_logging_stdout"
STDERR_HANDLER_NAME = "care_logging_stderr"

_STREAM_HANDLER_CLASSES = frozenset(
    {
        "logging.StreamHandler",
        "logging.handlers.StreamHandler",
    }
)

_LOGGER_SPLIT_MARKER = "_care_logging_stream_split"


class BelowErrorFilter(logging.Filter):
    """Allow only records strictly below ERROR (for stdout console handlers)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def _is_stream_handler(handler_cfg: dict[str, Any]) -> bool:
    handler_class = handler_cfg.get("class")
    if not isinstance(handler_class, str):
        return False
    if handler_class in _STREAM_HANDLER_CLASSES:
        return True
    return handler_class.endswith(".StreamHandler") or handler_class.endswith("StreamHandler")


def _formatter_name(handlers: dict[str, Any], fallback: str = "verbose") -> str:
    console = handlers.get("console") or {}
    name = console.get("formatter")
    return name if isinstance(name, str) and name else fallback


def _ensure_formatter(config: dict[str, Any], name: str) -> None:
    formatters = config.setdefault("formatters", {})
    if name in formatters:
        return
    formatters[name] = {
        "format": ("%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s"),
    }


def _ensure_below_error_filter(config: dict[str, Any]) -> None:
    filters = config.setdefault("filters", {})
    filters.setdefault(
        FILTER_NAME,
        {"()": "care_logging.logging_config.BelowErrorFilter"},
    )


def _append_filter(handler_cfg: dict[str, Any], filter_name: str) -> None:
    filters = list(handler_cfg.get("filters") or [])
    if filter_name not in filters:
        filters.append(filter_name)
    handler_cfg["filters"] = filters


def _ensure_stderr_handler(config: dict[str, Any], formatter: str) -> None:
    handlers = config.setdefault("handlers", {})
    handlers.setdefault(
        STDERR_HANDLER_NAME,
        {
            "level": "ERROR",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": formatter,
        },
    )


def _configure_handlers(config: dict[str, Any]) -> None:
    handlers = config.setdefault("handlers", {})
    formatter = _formatter_name(handlers)
    _ensure_formatter(config, formatter)
    _ensure_stderr_handler(config, formatter)

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
        _append_filter(console, FILTER_NAME)
    else:
        # Non-stream console (file/JSON/etc.): leave it alone and add a dedicated stdout sink.
        handlers.setdefault(
            STDOUT_HANDLER_NAME,
            {
                "level": console.get("level", "DEBUG"),
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": formatter,
                "filters": [FILTER_NAME],
            },
        )

    time_logging = handlers.get("time_logging")
    if isinstance(time_logging, dict) and _is_stream_handler(time_logging):
        time_logging["stream"] = "ext://sys.stdout"
        _append_filter(time_logging, FILTER_NAME)


def _stdout_handler_name(config: dict[str, Any]) -> str:
    handlers = config.get("handlers") or {}
    console = handlers.get("console")
    if isinstance(console, dict) and _is_stream_handler(console):
        return "console"
    if STDOUT_HANDLER_NAME in handlers:
        return STDOUT_HANDLER_NAME
    return "console"


def _configure_root(config: dict[str, Any]) -> None:
    root = config.setdefault("root", {"level": "INFO", "handlers": ["console"]})
    root_handlers = list(root.get("handlers") or [])
    stdout_handler = _stdout_handler_name(config)

    if stdout_handler not in root_handlers:
        root_handlers.insert(0, stdout_handler)
    if STDERR_HANDLER_NAME not in root_handlers:
        root_handlers.append(STDERR_HANDLER_NAME)

    root["handlers"] = root_handlers


def _reroute_error_loggers(config: dict[str, Any]) -> None:
    """Point ERROR loggers that use ``console`` at the stderr handler.

    Propagation is only changed when ``console`` was replaced and leaving
    propagate enabled would duplicate records via root's stderr handler.
    Loggers that do not use ``console`` are left untouched.
    """
    loggers = config.setdefault("loggers", {})
    for _name, logger_cfg in list(loggers.items()):
        if not isinstance(logger_cfg, dict):
            continue
        level = str(logger_cfg.get("level") or "").upper()
        if level != "ERROR":
            continue
        handlers = list(logger_cfg.get("handlers") or [])
        if "console" not in handlers:
            continue

        new_handlers = [STDERR_HANDLER_NAME if handler == "console" else handler for handler in handlers]
        logger_cfg["handlers"] = new_handlers

        # Default propagate is True when omitted.
        if logger_cfg.get("propagate", True):
            logger_cfg["propagate"] = False


def _ensure_time_logging_errors_reach_stderr(config: dict[str, Any]) -> None:
    """Keep time_logging INFO on stdout; route ERROR+ from that logger to stderr.

    Care's ``time_logging_middleware`` logger uses ``propagate=False`` with only
    the ``time_logging`` handler. Filtering that handler below ERROR would
    otherwise drop ERROR+ records entirely.
    """
    handlers = config.get("handlers") or {}
    if "time_logging" not in handlers:
        return
    loggers = config.setdefault("loggers", {})
    time_logger = loggers.get("time_logging_middleware")
    if not isinstance(time_logger, dict):
        return
    handler_names = list(time_logger.get("handlers") or [])
    if STDERR_HANDLER_NAME not in handler_names:
        handler_names.append(STDERR_HANDLER_NAME)
    time_logger["handlers"] = handler_names


def _get_settings_logging() -> dict[str, Any] | None:
    """Return ``settings.LOGGING`` when Django is configured; otherwise ``None``."""
    try:
        if not settings.configured:
            return None
        value = getattr(settings, "LOGGING", None)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def build_split_logging_config(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a LOGGING dict with the stdout/stderr split applied.

    Raises on deepcopy failure so callers can abort without applying a blank
    replacement. When *base* is ``None`` and ``settings.LOGGING`` is empty,
    constructs a minimal fallback with ``disable_existing_loggers=False``.
    """
    if base is not None:
        source = base
    else:
        source = _get_settings_logging()

    if source:
        config = copy.deepcopy(source)
    else:
        config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {},
            "handlers": {},
            "root": {"level": "INFO", "handlers": ["console"]},
        }

    config["version"] = config.get("version", 1)
    # Preserve Care's value (base/test=False, deployment=True). Only the empty
    # fallback above introduces False explicitly.
    if "disable_existing_loggers" not in config:
        config["disable_existing_loggers"] = False

    _ensure_below_error_filter(config)
    _configure_handlers(config)
    _configure_root(config)
    _reroute_error_loggers(config)
    _ensure_time_logging_errors_reach_stderr(config)
    return config


def _handler_has_below_error_filter(handler: logging.Handler) -> bool:
    return any(isinstance(filt, BelowErrorFilter) for filt in handler.filters)


def _is_stdout_stream(handler: logging.Handler) -> bool:
    stream = getattr(handler, "stream", None)
    return stream in (sys.stdout, sys.__stdout__)


def _is_stderr_stream(handler: logging.Handler) -> bool:
    stream = getattr(handler, "stream", None)
    return stream in (sys.stderr, sys.__stderr__)


def _is_split_active_on_logger(target: logging.Logger) -> bool:
    has_stdout = False
    has_stderr = False
    for handler in target.handlers:
        if not isinstance(handler, logging.StreamHandler):
            continue
        if _handler_has_below_error_filter(handler) and _is_stdout_stream(handler):
            has_stdout = True
        if handler.level >= logging.ERROR and _is_stderr_stream(handler):
            has_stderr = True
    return has_stdout and has_stderr


def _restore_logging_config(original: dict[str, Any]) -> bool:
    try:
        logging.config.dictConfig(original)
        return True
    except Exception:
        logger.exception("care_logging: failed to restore the original logging configuration")
        return False


def apply_split_console_logging(*, force: bool = False) -> bool:
    """Reconfigure process logging from Care's LOGGING settings.

    Returns True when configuration was applied. Failures are logged and do not
    raise, so a bad transform cannot take down Care startup.

    Idempotent: repeated calls are no-ops while the split is already active on
    the root logger. Celery may clear root handlers later; use
    :func:`apply_celery_stream_split` from Celery signals to re-establish the
    split without relying on a process-global "already applied" flag.
    """
    with _apply_lock:
        if not force and _is_split_active_on_logger(logging.getLogger()):
            return False

        original: dict[str, Any] | None
        try:
            source = _get_settings_logging()
            original = copy.deepcopy(source) if source else None
        except Exception:
            logger.warning(
                "care_logging: could not copy settings.LOGGING; aborting without changes",
                exc_info=True,
            )
            return False

        try:
            config = build_split_logging_config(original)
        except Exception:
            logger.exception(
                "care_logging: failed to build split logging config; aborting without changes"
            )
            return False

        try:
            logging.config.dictConfig(config)
            logger.info("care_logging: applied stdout/stderr console split")
            return True
        except Exception:
            logger.exception("care_logging: failed to apply split console logging")
            if original is not None:
                if _restore_logging_config(original):
                    logger.error(
                        "care_logging: restored the original Care logging configuration after apply failure"
                    )
                else:
                    logger.error(
                        "care_logging: apply failed and the original logging configuration "
                        "could not be restored"
                    )
            else:
                logger.error(
                    "care_logging: apply failed; no original Care LOGGING dict was available to restore"
                )
            return False


def _make_stream_handler(
    stream: Any,
    *,
    level: int,
    fmt: str | None,
    filters: list[logging.Filter] | None = None,
) -> logging.StreamHandler:
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    if fmt:
        handler.setFormatter(logging.Formatter(fmt))
    for filt in filters or []:
        handler.addFilter(filt)
    return handler


def apply_celery_stream_split(
    target: logging.Logger | None,
    *,
    loglevel: int | None = None,
    fmt: str | None = None,
    logfile: Any = None,
) -> bool:
    """Split an already-configured Celery logger across stdout/stderr.

    No-op when Celery is logging to a file (``logfile`` set) or when the logger
    already has the plug's stream split. Failures are logged and do not raise.
    """
    if target is None:
        return False
    if logfile:
        # File logging is a single sink; do not invent a stream split.
        return False

    with _apply_lock:
        prior_handlers = list(target.handlers)
        try:
            if _is_split_active_on_logger(target):
                return False

            level = loglevel if loglevel is not None else target.level or logging.INFO
            if isinstance(level, str):
                level = logging.getLevelName(level)
            if not isinstance(level, int):
                level = logging.INFO

            # Build replacements first so a construction failure leaves the
            # logger untouched.
            preserved = [h for h in prior_handlers if not isinstance(h, logging.StreamHandler)]
            to_replace = [h for h in prior_handlers if isinstance(h, logging.StreamHandler)]
            stdout_handler = _make_stream_handler(
                sys.stdout,
                level=level,
                fmt=fmt,
                filters=[BelowErrorFilter()],
            )
            stderr_handler = _make_stream_handler(
                sys.stderr,
                level=max(level, logging.ERROR),
                fmt=fmt,
            )

            target.handlers = []
            for handler in preserved:
                target.addHandler(handler)
            target.addHandler(stdout_handler)
            target.addHandler(stderr_handler)
            if level:
                target.setLevel(level)
            setattr(target, _LOGGER_SPLIT_MARKER, True)

            for handler in to_replace:
                try:
                    handler.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
            return True
        except Exception:
            target.handlers = list(prior_handlers)
            logger.exception(
                "care_logging: failed to apply Celery stream split on logger %s; "
                "leaving Celery's logging configuration in place",
                getattr(target, "name", target),
            )
            return False


def _on_after_setup_logger(
    sender=None,
    logger=None,
    loglevel=None,
    logfile=None,
    format=None,
    **_kwargs,
) -> None:
    apply_celery_stream_split(logger, loglevel=loglevel, fmt=format, logfile=logfile)

    # Celery clears handlers on the ``celery`` logger during hijack. If Care's
    # LOGGING left it at propagate=False, records would be dropped — restore
    # propagation when it has no handlers so they reach the split root.
    celery_logger = logging.getLogger("celery")
    if not celery_logger.handlers and not celery_logger.propagate:
        celery_logger.propagate = True


def _on_after_setup_task_logger(
    sender=None,
    logger=None,
    loglevel=None,
    logfile=None,
    format=None,
    **_kwargs,
) -> None:
    # Task logger defaults to propagate=False; keep that and split its own handlers
    # so task records are not duplicated via root.
    apply_celery_stream_split(logger, loglevel=loglevel, fmt=format, logfile=logfile)


def install_celery_logging_hooks() -> bool:
    """Connect Celery ``after_setup_*`` receivers once.

    Soft-imports Celery so Django/Gunicorn deployments without Celery installed
    are unaffected. Returns True when hooks were (already) installed.
    """
    global _celery_hooks_installed
    if _celery_hooks_installed:
        return True
    try:
        from celery.signals import after_setup_logger, after_setup_task_logger
    except ImportError:
        logger.debug("care_logging: Celery not installed; skipping Celery logging hooks")
        return False

    after_setup_logger.connect(
        _on_after_setup_logger,
        weak=False,
        dispatch_uid="care_logging.after_setup_logger",
    )
    after_setup_task_logger.connect(
        _on_after_setup_task_logger,
        weak=False,
        dispatch_uid="care_logging.after_setup_task_logger",
    )
    _celery_hooks_installed = True
    logger.debug("care_logging: installed Celery after_setup logging hooks")
    return True


def reset_apply_state() -> None:
    """Test helper to clear idempotency markers and Celery hook state."""
    global _celery_hooks_installed
    with _apply_lock:
        _celery_hooks_installed = False
        root = logging.getLogger()
        if hasattr(root, _LOGGER_SPLIT_MARKER):
            delattr(root, _LOGGER_SPLIT_MARKER)
        for name in ("celery", "celery.task"):
            log = logging.getLogger(name)
            if hasattr(log, _LOGGER_SPLIT_MARKER):
                delattr(log, _LOGGER_SPLIT_MARKER)
