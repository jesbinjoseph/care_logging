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
from collections.abc import Callable
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

_apply_lock = threading.Lock()
_celery_hooks_installed = False

# Preferred plug-specific dictConfig names — never overwrite Care's generic keys.
FILTER_NAME = "care_logging_below_error"
STDOUT_HANDLER_NAME = "care_logging_stdout"
STDERR_HANDLER_NAME = "care_logging_stderr"

_BELOW_ERROR_FILTER_FACTORY = "care_logging.logging_config.BelowErrorFilter"

_STREAM_HANDLER_CLASSES = frozenset(
    {
        "logging.StreamHandler",
        "logging.handlers.StreamHandler",
    }
)

_LOGGER_SPLIT_MARKER = "_care_logging_stream_split"
_CELERY_LOGGER_PREFIXES = ("celery",)


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


def _level_at_least_error(level: Any) -> bool:
    if isinstance(level, int):
        return level >= logging.ERROR
    if not isinstance(level, str):
        return False
    name = level.upper()
    if name in {"ERROR", "CRITICAL", "FATAL"}:
        return True
    numeric = getattr(logging, name, None)
    return isinstance(numeric, int) and numeric >= logging.ERROR


def _is_compatible_below_error_filter(cfg: Any) -> bool:
    return isinstance(cfg, dict) and cfg.get("()") == _BELOW_ERROR_FILTER_FACTORY


def _is_compatible_stderr_handler(cfg: Any, formatter: str) -> bool:
    if not isinstance(cfg, dict) or not _is_stream_handler(cfg):
        return False
    if cfg.get("stream") != "ext://sys.stderr":
        return False
    if not _level_at_least_error(cfg.get("level", "ERROR")):
        return False
    existing_formatter = cfg.get("formatter")
    return existing_formatter in (None, formatter)


def _is_compatible_stdout_handler(cfg: Any, formatter: str, filter_name: str) -> bool:
    if not isinstance(cfg, dict) or not _is_stream_handler(cfg):
        return False
    if cfg.get("stream") != "ext://sys.stdout":
        return False
    filters = list(cfg.get("filters") or [])
    if filter_name not in filters:
        return False
    existing_formatter = cfg.get("formatter")
    return existing_formatter in (None, formatter)


def _claim_resource_name(
    mapping: dict[str, Any],
    preferred: str,
    is_compatible: Callable[[Any], bool],
    *,
    kind: str,
) -> str:
    """Return *preferred*, replacing an incompatible pre-existing plug entry.

    Care generic names (``below_error``, ``console_error``, …) are never used as
    *preferred*, so they remain untouched. An incompatible entry already using a
    plug-namespaced key cannot be left in place — ``dictConfig`` would fail — so
    it is replaced after a warning.
    """
    existing = mapping.get(preferred)
    if existing is None or is_compatible(existing):
        return preferred
    logger.warning(
        "care_logging: replacing incompatible existing %s %r so logging can be configured",
        kind,
        preferred,
    )
    del mapping[preferred]
    return preferred


def _append_filter(handler_cfg: dict[str, Any], filter_name: str) -> None:
    filters = list(handler_cfg.get("filters") or [])
    if filter_name not in filters:
        filters.append(filter_name)
    handler_cfg["filters"] = filters


def _resolve_plug_names(config: dict[str, Any], formatter: str) -> tuple[str, str, str]:
    """Return (filter_name, stdout_handler_name, stderr_handler_name) for this config."""
    filters = config.setdefault("filters", {})
    handlers = config.setdefault("handlers", {})

    filter_name = _claim_resource_name(
        filters,
        FILTER_NAME,
        _is_compatible_below_error_filter,
        kind="filter",
    )
    stderr_name = _claim_resource_name(
        handlers,
        STDERR_HANDLER_NAME,
        lambda cfg: _is_compatible_stderr_handler(cfg, formatter),
        kind="handler",
    )
    stdout_name = _claim_resource_name(
        handlers,
        STDOUT_HANDLER_NAME,
        lambda cfg: _is_compatible_stdout_handler(cfg, formatter, filter_name),
        kind="handler",
    )
    return filter_name, stdout_name, stderr_name


def _configure_handlers(
    config: dict[str, Any],
    *,
    filter_name: str,
    stdout_name: str,
    stderr_name: str,
    formatter: str,
) -> None:
    handlers = config.setdefault("handlers", {})
    filters = config.setdefault("filters", {})

    if not _is_compatible_below_error_filter(filters.get(filter_name)):
        filters[filter_name] = {"()": _BELOW_ERROR_FILTER_FACTORY}

    if not _is_compatible_stderr_handler(handlers.get(stderr_name), formatter):
        handlers[stderr_name] = {
            "level": "ERROR",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": formatter,
        }

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
        _append_filter(console, filter_name)
    else:
        # Non-stream console (file/JSON/etc.): leave it alone and add a dedicated stdout sink.
        if not _is_compatible_stdout_handler(handlers.get(stdout_name), formatter, filter_name):
            handlers[stdout_name] = {
                "level": console.get("level", "DEBUG"),
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": formatter,
                "filters": [filter_name],
            }

    time_logging = handlers.get("time_logging")
    if isinstance(time_logging, dict) and _is_stream_handler(time_logging):
        time_logging["stream"] = "ext://sys.stdout"
        _append_filter(time_logging, filter_name)


def _stdout_handler_name(config: dict[str, Any], allocated_stdout: str) -> str:
    handlers = config.get("handlers") or {}
    console = handlers.get("console")
    if isinstance(console, dict) and _is_stream_handler(console):
        return "console"
    if allocated_stdout in handlers:
        return allocated_stdout
    return "console"


def _configure_root(config: dict[str, Any], *, stdout_name: str, stderr_name: str) -> None:
    root = config.setdefault("root", {"level": "INFO", "handlers": ["console"]})
    root_handlers = list(root.get("handlers") or [])
    stdout_handler = _stdout_handler_name(config, stdout_name)

    if stdout_handler not in root_handlers:
        root_handlers.insert(0, stdout_handler)
    if stderr_name not in root_handlers:
        root_handlers.append(stderr_name)

    root["handlers"] = root_handlers


def _handler_has_named_filter(handler_cfg: Any, filter_name: str) -> bool:
    return isinstance(handler_cfg, dict) and filter_name in list(handler_cfg.get("filters") or [])


def _level_is_error_or_above(level: str) -> bool:
    return level in {"ERROR", "CRITICAL", "FATAL"}


def _ensure_named_loggers_reach_stderr(
    config: dict[str, Any],
    *,
    stderr_name: str,
    filter_name: str,
) -> None:
    """Keep ERROR+ visible for named loggers after console is filtered below ERROR.

    - ERROR/CRITICAL loggers that use ``console`` are pointed at the stderr
      handler (console would emit nothing useful once filtered). Propagation is
      only disabled when that replacement would otherwise duplicate via root.
    - Any non-propagating logger whose handlers include a below-ERROR filter
      (``console``, ``time_logging``, …) gets the stderr handler appended so
      ERROR+ records are not silently dropped.
    - Loggers that do not use a filtered console/stream handler are untouched.
    """
    handlers_cfg = config.get("handlers") or {}
    console = handlers_cfg.get("console")
    console_is_filtered_stream = (
        isinstance(console, dict)
        and _is_stream_handler(console)
        and _handler_has_named_filter(console, filter_name)
    )

    loggers = config.setdefault("loggers", {})
    for _name, logger_cfg in list(loggers.items()):
        if not isinstance(logger_cfg, dict):
            continue
        handler_names = list(logger_cfg.get("handlers") or [])
        if not handler_names:
            continue

        level = str(logger_cfg.get("level") or "").upper()

        # ERROR+ only loggers: replace filtered console with stderr.
        if (
            _level_is_error_or_above(level)
            and "console" in handler_names
            and console_is_filtered_stream
        ):
            logger_cfg["handlers"] = [
                stderr_name if handler == "console" else handler for handler in handler_names
            ]
            if logger_cfg.get("propagate", True):
                logger_cfg["propagate"] = False
            continue

        # Non-propagating loggers with a below-ERROR filtered handler would
        # otherwise drop ERROR+ (e.g. level=INFO, handlers=[console], propagate=False).
        if logger_cfg.get("propagate", True):
            continue
        has_filtered_handler = any(
            _handler_has_named_filter(handlers_cfg.get(handler), filter_name)
            for handler in handler_names
        )
        if has_filtered_handler and stderr_name not in handler_names:
            handler_names.append(stderr_name)
            logger_cfg["handlers"] = handler_names


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

    handlers = config.setdefault("handlers", {})
    formatter = _formatter_name(handlers)
    _ensure_formatter(config, formatter)
    filter_name, stdout_name, stderr_name = _resolve_plug_names(config, formatter)

    _configure_handlers(
        config,
        filter_name=filter_name,
        stdout_name=stdout_name,
        stderr_name=stderr_name,
        formatter=formatter,
    )
    _configure_root(config, stdout_name=stdout_name, stderr_name=stderr_name)
    _ensure_named_loggers_reach_stderr(
        config,
        stderr_name=stderr_name,
        filter_name=filter_name,
    )
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


def _existing_stream_formatter(
    handlers: list[logging.Handler],
) -> logging.Formatter | None:
    """Return the formatter from an existing stream handler, if any.

    Preserves Celery's ``TaskFormatter`` so ``%(task_name)s`` / ``%(task_id)s``
    keep working after the stream split.
    """
    for handler in handlers:
        if isinstance(handler, logging.StreamHandler) and handler.formatter is not None:
            return handler.formatter
    return None


def _make_stream_handler(
    stream: Any,
    *,
    level: int,
    formatter: logging.Formatter | None = None,
    fmt: str | None = None,
    filters: list[logging.Filter] | None = None,
) -> logging.StreamHandler:
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    if formatter is not None:
        handler.setFormatter(formatter)
    elif fmt:
        handler.setFormatter(logging.Formatter(fmt))
    for filt in filters or []:
        handler.addFilter(filt)
    return handler


def _reenable_celery_loggers() -> None:
    """Clear ``disabled`` on Celery loggers left behind by ``disable_existing_loggers``.

    Care deployment sets ``disable_existing_loggers=True``. If Celery loggers
    existed before Django's ``dictConfig``, they remain ``disabled=True`` even
    after Celery reinstalls handlers.
    """
    manager = logging.Logger.manager
    for name, candidate in list(manager.loggerDict.items()):
        if not any(name == prefix or name.startswith(f"{prefix}.") for prefix in _CELERY_LOGGER_PREFIXES):
            continue
        if isinstance(candidate, logging.PlaceHolder):
            continue
        if isinstance(candidate, logging.Logger):
            candidate.disabled = False
    for prefix in _CELERY_LOGGER_PREFIXES:
        logging.getLogger(prefix).disabled = False
        # Ensure the task logger used by Celery is enabled even if not yet created.
        logging.getLogger(f"{prefix}.task").disabled = False


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
                target.disabled = False
                return False

            level = loglevel if loglevel is not None else target.level or logging.INFO
            if isinstance(level, str):
                level = logging.getLevelName(level)
            if not isinstance(level, int):
                level = logging.INFO

            # Build replacements first so a construction failure leaves the
            # logger untouched. Reuse Celery's formatter instance (TaskFormatter).
            preserved = [h for h in prior_handlers if not isinstance(h, logging.StreamHandler)]
            to_replace = [h for h in prior_handlers if isinstance(h, logging.StreamHandler)]
            existing_formatter = _existing_stream_formatter(to_replace)
            stdout_handler = _make_stream_handler(
                sys.stdout,
                level=level,
                formatter=existing_formatter,
                fmt=None if existing_formatter is not None else fmt,
                filters=[BelowErrorFilter()],
            )
            stderr_handler = _make_stream_handler(
                sys.stderr,
                level=max(level, logging.ERROR),
                formatter=existing_formatter,
                fmt=None if existing_formatter is not None else fmt,
            )

            target.handlers = []
            for handler in preserved:
                target.addHandler(handler)
            target.addHandler(stdout_handler)
            target.addHandler(stderr_handler)
            if level:
                target.setLevel(level)
            target.disabled = False
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
    _reenable_celery_loggers()

    # Celery clears handlers on the ``celery`` logger during hijack. If Care's
    # LOGGING left it at propagate=False, records would be dropped — restore
    # propagation when it has no handlers so they reach the split root.
    celery_logger = logging.getLogger("celery")
    if not celery_logger.handlers and not celery_logger.propagate:
        celery_logger.propagate = True
    celery_logger.disabled = False


def _on_after_setup_task_logger(
    sender=None,
    logger=None,
    loglevel=None,
    logfile=None,
    format=None,
    **_kwargs,
) -> None:
    # Task logger defaults to propagate=False; keep that and split its own handlers
    # so task records are not duplicated via root. Preserve TaskFormatter via the
    # existing handler's formatter instance.
    apply_celery_stream_split(logger, loglevel=loglevel, fmt=format, logfile=logfile)
    _reenable_celery_loggers()
    if logger is not None:
        logger.disabled = False


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
            log.disabled = False
