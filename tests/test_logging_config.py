"""Tests for care_logging configuration transforms and runtime behavior."""

from __future__ import annotations

import copy
import io
import logging
import logging.config
import sys
from unittest import mock

import pytest

from care_logging.logging_config import (
    FILTER_NAME,
    STDERR_HANDLER_NAME,
    STDOUT_HANDLER_NAME,
    BelowErrorFilter,
    apply_celery_stream_split,
    apply_split_console_logging,
    build_split_logging_config,
    install_celery_logging_hooks,
    reset_apply_state,
)
from tests.care_fixtures import (
    CARE_BASE_LOGGING,
    CARE_DEPLOYMENT_LOGGING,
    CARE_TEST_LOGGING,
)


@pytest.fixture(autouse=True)
def _reset_apply_guard():
    reset_apply_state()
    yield
    reset_apply_state()


@pytest.fixture
def isolated_logging():
    """Snapshot/restore root logger handlers so tests do not leak state."""
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    named = {}
    for name in (
        "django.request",
        "django.db.backends",
        "sentry_sdk",
        "audit_log",
        "celery",
        "celery.task",
        "time_logging_middleware",
        "custom.error",
        "care.app",
    ):
        log = logging.getLogger(name)
        named[name] = (list(log.handlers), log.level, log.propagate)
    yield
    root.handlers.clear()
    for handler in old_handlers:
        root.addHandler(handler)
    root.setLevel(old_level)
    for name, (handlers, level, propagate) in named.items():
        log = logging.getLogger(name)
        log.handlers.clear()
        for handler in handlers:
            log.addHandler(handler)
        log.setLevel(level)
        log.propagate = propagate


def test_below_error_filter():
    filt = BelowErrorFilter()
    info = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", (), None)
    warning = logging.LogRecord("n", logging.WARNING, __file__, 1, "msg", (), None)
    error = logging.LogRecord("n", logging.ERROR, __file__, 1, "msg", (), None)
    critical = logging.LogRecord("n", logging.CRITICAL, __file__, 1, "msg", (), None)
    assert filt.filter(info) is True
    assert filt.filter(warning) is True
    assert filt.filter(error) is False
    assert filt.filter(critical) is False


def test_exact_care_base_logging_transform():
    config = build_split_logging_config(copy.deepcopy(CARE_BASE_LOGGING))

    assert config["disable_existing_loggers"] is False
    assert FILTER_NAME in config["filters"]
    assert "below_error" not in config["filters"]
    assert "console_error" not in config["handlers"]
    assert config["handlers"]["console"]["stream"] == "ext://sys.stdout"
    assert FILTER_NAME in config["handlers"]["console"]["filters"]
    assert config["handlers"][STDERR_HANDLER_NAME]["stream"] == "ext://sys.stderr"
    assert config["handlers"][STDERR_HANDLER_NAME]["level"] == "ERROR"
    assert config["root"]["handlers"] == ["console", STDERR_HANDLER_NAME]
    assert config["handlers"]["time_logging"]["stream"] == "ext://sys.stdout"
    assert FILTER_NAME in config["handlers"]["time_logging"]["filters"]
    assert config["loggers"]["time_logging_middleware"]["handlers"] == [
        "time_logging",
        STDERR_HANDLER_NAME,
    ]
    assert config["loggers"]["time_logging_middleware"]["propagate"] is False


def test_exact_care_deployment_logging_preserves_disable_existing_loggers():
    config = build_split_logging_config(copy.deepcopy(CARE_DEPLOYMENT_LOGGING))

    assert config["disable_existing_loggers"] is True
    assert config["loggers"]["django.db.backends"]["handlers"] == [STDERR_HANDLER_NAME]
    assert config["loggers"]["django.db.backends"]["propagate"] is False
    assert config["loggers"]["sentry_sdk"]["handlers"] == [STDERR_HANDLER_NAME]
    assert config["loggers"]["sentry_sdk"]["propagate"] is False


def test_exact_care_test_logging_reroutes_error_loggers():
    config = build_split_logging_config(copy.deepcopy(CARE_TEST_LOGGING))

    assert config["disable_existing_loggers"] is False
    for name in ("django.request", "audit_log", "celery"):
        assert config["loggers"][name]["handlers"] == [STDERR_HANDLER_NAME]
        assert config["loggers"][name]["propagate"] is False


def test_fallback_config_uses_disable_existing_loggers_false():
    config = build_split_logging_config({})
    assert config["disable_existing_loggers"] is False
    assert "console" in config["handlers"]
    assert STDERR_HANDLER_NAME in config["handlers"]


def test_unrelated_error_logger_with_non_console_handler_unchanged():
    base = copy.deepcopy(CARE_DEPLOYMENT_LOGGING)
    base["handlers"]["file"] = {
        "level": "ERROR",
        "class": "logging.FileHandler",
        "filename": "/tmp/care-logging-unrelated.log",
        "formatter": "verbose",
    }
    base["loggers"]["custom.error"] = {
        "level": "ERROR",
        "handlers": ["file"],
        "propagate": True,
    }
    original = copy.deepcopy(base["loggers"]["custom.error"])
    config = build_split_logging_config(base)
    assert config["loggers"]["custom.error"] == original


def test_error_logger_preserves_extra_handlers_and_order():
    base = copy.deepcopy(CARE_TEST_LOGGING)
    base["handlers"]["file"] = {
        "level": "ERROR",
        "class": "logging.FileHandler",
        "filename": "/tmp/care-logging-extra.log",
        "formatter": "verbose",
    }
    base["loggers"]["django.request"] = {
        "level": "ERROR",
        "handlers": ["file", "console"],
        "propagate": False,
    }
    config = build_split_logging_config(base)
    assert config["loggers"]["django.request"]["handlers"] == ["file", STDERR_HANDLER_NAME]
    assert config["loggers"]["django.request"]["propagate"] is False


def test_preserves_existing_custom_filters_and_non_console_handlers():
    base = copy.deepcopy(CARE_BASE_LOGGING)
    base["filters"] = {
        "below_error": {"()": "django.utils.log.CallbackFilter", "callback": lambda r: True},
        "request_id": {"()": "django.utils.log.CallbackFilter", "callback": lambda r: True},
    }
    base["handlers"]["json"] = {
        "level": "INFO",
        "class": "logging.FileHandler",
        "filename": "/tmp/care-logging-json.log",
        "formatter": "verbose",
        "filters": ["request_id"],
    }
    base["root"]["handlers"] = ["console", "json"]
    config = build_split_logging_config(base)

    assert "below_error" in config["filters"]
    assert "request_id" in config["filters"]
    assert FILTER_NAME in config["filters"]
    assert config["handlers"]["json"]["filters"] == ["request_id"]
    assert "json" in config["root"]["handlers"]
    assert STDERR_HANDLER_NAME in config["root"]["handlers"]


def test_does_not_overwrite_existing_plug_or_generic_names():
    base = copy.deepcopy(CARE_BASE_LOGGING)
    base["filters"] = {
        "below_error": {"marker": "care-original"},
        FILTER_NAME: {"marker": "preexisting-plug-filter"},
    }
    base["handlers"]["console_error"] = {"marker": "care-original-handler"}
    base["handlers"][STDERR_HANDLER_NAME] = {"marker": "preexisting-plug-handler"}
    config = build_split_logging_config(base)

    assert config["filters"]["below_error"] == {"marker": "care-original"}
    assert config["filters"][FILTER_NAME] == {"marker": "preexisting-plug-filter"}
    assert config["handlers"]["console_error"] == {"marker": "care-original-handler"}
    assert config["handlers"][STDERR_HANDLER_NAME] == {"marker": "preexisting-plug-handler"}


def test_build_is_idempotent():
    once = build_split_logging_config(copy.deepcopy(CARE_BASE_LOGGING))
    twice = build_split_logging_config(once)
    assert twice["handlers"]["console"]["filters"].count(FILTER_NAME) == 1
    assert twice["root"]["handlers"].count(STDERR_HANDLER_NAME) == 1
    assert twice["root"]["handlers"].count("console") == 1
    assert twice["loggers"]["time_logging_middleware"]["handlers"].count(STDERR_HANDLER_NAME) == 1


def test_non_stream_console_is_preserved():
    base = copy.deepcopy(CARE_BASE_LOGGING)
    base["handlers"]["console"] = {
        "level": "INFO",
        "class": "logging.FileHandler",
        "filename": "/tmp/care-logging-test.log",
        "formatter": "verbose",
    }
    config = build_split_logging_config(base)
    assert config["handlers"]["console"]["class"] == "logging.FileHandler"
    assert "stream" not in config["handlers"]["console"]
    assert STDOUT_HANDLER_NAME in config["handlers"]
    assert config["handlers"][STDOUT_HANDLER_NAME]["stream"] == "ext://sys.stdout"
    assert STDERR_HANDLER_NAME in config["handlers"]
    assert STDOUT_HANDLER_NAME in config["root"]["handlers"]
    assert "console" in config["root"]["handlers"]


def test_live_root_routes_all_levels(monkeypatch, isolated_logging):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    config = build_split_logging_config(copy.deepcopy(CARE_BASE_LOGGING))
    logging.config.dictConfig(config)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.debug("lvl-debug")
    root.info("lvl-info")
    root.warning("lvl-warning")
    root.error("lvl-error")
    root.critical("lvl-critical")

    out = stdout.getvalue()
    err = stderr.getvalue()
    for msg in ("lvl-debug", "lvl-info", "lvl-warning"):
        assert msg in out
        assert msg not in err
    for msg in ("lvl-error", "lvl-critical"):
        assert msg in err
        assert msg not in out


def test_named_error_logger_no_duplication(monkeypatch, isolated_logging):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    config = build_split_logging_config(copy.deepcopy(CARE_TEST_LOGGING))
    logging.config.dictConfig(config)

    logging.getLogger("django.request").error("request-failed")
    assert stderr.getvalue().count("request-failed") == 1
    assert "request-failed" not in stdout.getvalue()


def test_time_logging_middleware_error_not_dropped(monkeypatch, isolated_logging):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    config = build_split_logging_config(copy.deepcopy(CARE_BASE_LOGGING))
    logging.config.dictConfig(config)

    log = logging.getLogger("time_logging_middleware")
    log.info("request-took-12ms")
    log.error("request-timing-failed")

    assert "request-took-12ms" in stdout.getvalue()
    assert "request-timing-failed" not in stdout.getvalue()
    assert stderr.getvalue().count("request-timing-failed") == 1
    assert "request-took-12ms" not in stderr.getvalue()


def test_apply_is_idempotent(isolated_logging):
    config = build_split_logging_config(copy.deepcopy(CARE_BASE_LOGGING))
    with mock.patch(
        "care_logging.logging_config._get_settings_logging",
        return_value=copy.deepcopy(CARE_BASE_LOGGING),
    ):
        with mock.patch(
            "care_logging.logging_config.build_split_logging_config",
            return_value=config,
        ):
            assert apply_split_console_logging() is True
            assert apply_split_console_logging() is False
            assert apply_split_console_logging(force=True) is True


def test_deepcopy_failure_aborts_without_changes(isolated_logging):
    root = logging.getLogger()
    before = list(root.handlers)

    with mock.patch(
        "care_logging.logging_config._get_settings_logging",
        return_value=CARE_BASE_LOGGING,
    ):
        with mock.patch(
            "care_logging.logging_config.copy.deepcopy",
            side_effect=RuntimeError("uncopyable"),
        ):
            assert apply_split_console_logging(force=True) is False

    assert list(root.handlers) == before


def test_dictconfig_failure_restores_original(isolated_logging):
    original = copy.deepcopy(CARE_BASE_LOGGING)
    logging.config.dictConfig(original)
    root = logging.getLogger()
    assert len(root.handlers) == 1

    restored_configs: list[dict] = []
    real_dict_config = logging.config.dictConfig

    def fake_dict_config(config):
        if STDERR_HANDLER_NAME in (config.get("handlers") or {}):
            raise RuntimeError("dictConfig boom")
        restored_configs.append(config)
        return real_dict_config(config)

    with mock.patch(
        "care_logging.logging_config._get_settings_logging",
        return_value=original,
    ):
        with mock.patch(
            "care_logging.logging_config.logging.config.dictConfig",
            side_effect=fake_dict_config,
        ):
            assert apply_split_console_logging(force=True) is False

    assert restored_configs and restored_configs[0]["root"]["handlers"] == ["console"]
    assert STDERR_HANDLER_NAME not in restored_configs[0].get("handlers", {})


def test_dictconfig_failure_reports_when_restore_also_fails(isolated_logging, caplog):
    original = copy.deepcopy(CARE_BASE_LOGGING)

    with mock.patch(
        "care_logging.logging_config._get_settings_logging",
        return_value=original,
    ):
        with mock.patch(
            "care_logging.logging_config.logging.config.dictConfig",
            side_effect=RuntimeError("always fails"),
        ):
            with caplog.at_level(logging.ERROR):
                assert apply_split_console_logging(force=True) is False

    assert any("could not be restored" in r.getMessage() for r in caplog.records)


def test_django_appconfig_ready_applies_split(monkeypatch, isolated_logging):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    import care_logging
    from care_logging.apps import CareLoggingConfig

    with mock.patch(
        "care_logging.logging_config._get_settings_logging",
        return_value=copy.deepcopy(CARE_BASE_LOGGING),
    ):
        app = CareLoggingConfig("care_logging", care_logging)
        app.ready()

    root = logging.getLogger()
    root.info("django-ready-info")
    root.error("django-ready-error")

    assert "django-ready-info" in stdout.getvalue()
    assert "django-ready-error" in stderr.getvalue()
    assert apply_split_console_logging() is False
    # Celery hooks should be installed from ready() when Celery is present.
    assert install_celery_logging_hooks() is True


def test_celery_stream_split_after_hijack(monkeypatch, isolated_logging):
    pytest.importorskip("celery")
    from celery.signals import after_setup_logger, after_setup_task_logger

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    # Simulate Django apply first.
    logging.config.dictConfig(build_split_logging_config(copy.deepcopy(CARE_BASE_LOGGING)))
    assert apply_split_console_logging() is False

    # Celery hijack: clear root / celery handlers and install a stderr handler.
    root = logging.getLogger()
    root.handlers = []
    celery_logger = logging.getLogger("celery")
    celery_logger.handlers = []
    celery_logger.propagate = False  # as left by Care test LOGGING + our reroute
    task_logger = logging.getLogger("celery.task")
    task_logger.handlers = []

    hijack = logging.StreamHandler(sys.stderr)
    hijack.setLevel(logging.INFO)
    hijack.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(hijack)

    assert install_celery_logging_hooks() is True

    after_setup_logger.send(
        sender=None,
        logger=root,
        loglevel=logging.INFO,
        logfile=None,
        format="%(levelname)s %(message)s",
        colorize=False,
    )
    task_logger.propagate = False
    task_handler = logging.StreamHandler(sys.stderr)
    task_handler.setLevel(logging.INFO)
    task_logger.addHandler(task_handler)
    after_setup_task_logger.send(
        sender=None,
        logger=task_logger,
        loglevel=logging.INFO,
        logfile=None,
        format="%(levelname)s %(message)s",
        colorize=False,
    )

    # Hijack cleared celery handlers; hook should restore propagate.
    assert celery_logger.propagate is True

    root.info("celery-root-info")
    root.warning("celery-root-warning")
    root.error("celery-root-error")
    task_logger.info("celery-task-info")
    task_logger.error("celery-task-error")

    out = stdout.getvalue()
    err = stderr.getvalue()
    assert "celery-root-info" in out
    assert "celery-root-warning" in out
    assert "celery-root-error" not in out
    assert "celery-root-error" in err
    assert out.count("celery-root-info") == 1
    assert err.count("celery-root-error") == 1
    assert "celery-task-info" in out
    assert err.count("celery-task-error") == 1
    assert "celery-task-info" not in err
    # Task logger must not duplicate via root.
    assert out.count("celery-task-info") == 1

    # Idempotent: sending the signal again must not duplicate handlers.
    after_setup_logger.send(
        sender=None,
        logger=root,
        loglevel=logging.INFO,
        logfile=None,
        format="%(levelname)s %(message)s",
        colorize=False,
    )
    assert sum(1 for h in root.handlers if isinstance(h, logging.StreamHandler)) == 2


def test_celery_stream_split_skips_logfile(isolated_logging):
    target = logging.getLogger("celery")
    target.handlers = []
    handler = logging.StreamHandler(sys.stderr)
    target.addHandler(handler)
    assert apply_celery_stream_split(target, logfile="/tmp/celery.log") is False
    assert target.handlers == [handler]


def test_celery_stream_split_failure_is_safe(isolated_logging):
    target = logging.getLogger("celery.task")
    target.handlers = []
    original = logging.StreamHandler(sys.stderr)
    target.addHandler(original)

    with mock.patch(
        "care_logging.logging_config._make_stream_handler",
        side_effect=RuntimeError("boom"),
    ):
        assert apply_celery_stream_split(target, loglevel=logging.INFO) is False
    assert original in target.handlers


def test_repeated_build_and_apply_remain_idempotent(isolated_logging):
    base = copy.deepcopy(CARE_DEPLOYMENT_LOGGING)
    config = build_split_logging_config(base)
    again = build_split_logging_config(config)
    logging.config.dictConfig(again)
    assert apply_split_console_logging() is False
    assert again["filters"][FILTER_NAME]
    assert again["root"]["handlers"].count(STDERR_HANDLER_NAME) == 1
