"""Tests for care_logging configuration transforms and runtime behavior."""

from __future__ import annotations

import io
import logging
import logging.config
import sys
from unittest import mock

import pytest

from care_logging.logging_config import (
    BelowErrorFilter,
    apply_split_console_logging,
    build_split_logging_config,
    reset_apply_state,
)


@pytest.fixture(autouse=True)
def _reset_apply_guard():
    reset_apply_state()
    yield
    reset_apply_state()


def _care_like_base() -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {
                "format": "%(levelname)s %(asctime)s %(module)s %(message)s",
            }
        },
        "handlers": {
            "console": {
                "level": "DEBUG",
                "class": "logging.StreamHandler",
                "formatter": "verbose",
            }
        },
        "loggers": {
            "django.request": {
                "handlers": ["console"],
                "level": "ERROR",
            },
            "django.db.backends": {
                "handlers": ["console"],
                "level": "ERROR",
                "propagate": False,
            },
            "sentry_sdk": {
                "handlers": ["console"],
                "level": "ERROR",
                "propagate": False,
            },
            "audit_log": {
                "handlers": ["console"],
                "level": "ERROR",
            },
            "celery": {
                "handlers": ["console"],
                "level": "ERROR",
            },
            "care.app": {
                "handlers": ["console"],
                "level": "INFO",
            },
        },
        "root": {"level": "INFO", "handlers": ["console"]},
    }


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


def test_build_split_logging_config_matches_pr_intent():
    config = build_split_logging_config(_care_like_base())

    assert "below_error" in config["filters"]
    assert config["disable_existing_loggers"] is False
    assert config["handlers"]["console"]["stream"] == "ext://sys.stdout"
    assert "below_error" in config["handlers"]["console"]["filters"]
    assert config["handlers"]["console_error"]["stream"] == "ext://sys.stderr"
    assert config["handlers"]["console_error"]["level"] == "ERROR"
    assert config["root"]["handlers"] == ["console", "console_error"]

    for name in (
        "django.request",
        "django.db.backends",
        "sentry_sdk",
        "audit_log",
        "celery",
    ):
        assert config["loggers"][name]["handlers"] == ["console_error"]
        assert config["loggers"][name]["propagate"] is False

    assert config["loggers"]["care.app"]["handlers"] == ["console"]


def test_build_is_idempotent():
    once = build_split_logging_config(_care_like_base())
    twice = build_split_logging_config(once)
    assert twice["handlers"]["console"]["filters"].count("below_error") == 1
    assert twice["root"]["handlers"].count("console_error") == 1
    assert twice["root"]["handlers"].count("console") == 1


def test_time_logging_handler_gets_stdout_and_filter():
    base = _care_like_base()
    base["handlers"]["time_logging"] = {
        "level": "INFO",
        "class": "logging.StreamHandler",
        "formatter": "verbose",
    }
    config = build_split_logging_config(base)
    assert config["handlers"]["time_logging"]["stream"] == "ext://sys.stdout"
    assert "below_error" in config["handlers"]["time_logging"]["filters"]


def test_non_stream_console_is_preserved():
    base = _care_like_base()
    base["handlers"]["console"] = {
        "level": "INFO",
        "class": "logging.FileHandler",
        "filename": "/tmp/care-logging-test.log",
        "formatter": "verbose",
    }
    config = build_split_logging_config(base)
    assert config["handlers"]["console"]["class"] == "logging.FileHandler"
    assert "stream" not in config["handlers"]["console"]
    assert "care_logging_stdout" in config["handlers"]
    assert config["handlers"]["care_logging_stdout"]["stream"] == "ext://sys.stdout"
    assert "console_error" in config["handlers"]
    assert "care_logging_stdout" in config["root"]["handlers"]
    assert "console" in config["root"]["handlers"]


@pytest.fixture
def isolated_logging():
    """Apply configs without leaking handler state across tests."""
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    root.handlers.clear()
    for handler in old_handlers:
        root.addHandler(handler)
    root.setLevel(old_level)


def test_live_dictconfig_routes_info_and_error_streams(monkeypatch, isolated_logging):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    config = build_split_logging_config(_care_like_base())
    logging.config.dictConfig(config)

    root = logging.getLogger()
    root.info("hello-info")
    root.error("hello-error")

    out = stdout.getvalue()
    err = stderr.getvalue()
    assert "hello-info" in out
    assert "hello-error" not in out
    assert "hello-error" in err
    assert "hello-info" not in err


def test_error_logger_does_not_double_emit(monkeypatch, isolated_logging):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    config = build_split_logging_config(_care_like_base())
    logging.config.dictConfig(config)

    logging.getLogger("django.request").error("request-failed")
    assert stderr.getvalue().count("request-failed") == 1


def test_apply_is_idempotent_and_fail_safe(isolated_logging):
    with mock.patch(
        "care_logging.logging_config.build_split_logging_config",
        return_value=build_split_logging_config(_care_like_base()),
    ):
        assert apply_split_console_logging() is True
        assert apply_split_console_logging() is False

    reset_apply_state()
    with mock.patch(
        "care_logging.logging_config.build_split_logging_config",
        side_effect=RuntimeError("boom"),
    ):
        assert apply_split_console_logging() is False
