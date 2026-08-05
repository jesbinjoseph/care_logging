"""Compatibility checks against ohcnetwork/care's published LOGGING configs.

Full Care install is not required to validate this plug: Care's logging behavior
is defined by the LOGGING dict in settings. These tests fetch the latest
``develop`` settings modules from GitHub, extract ``LOGGING``, apply the plug
transform, and verify stdout/stderr routing.
"""

from __future__ import annotations

import ast
import io
import logging
import logging.config
import sys
import urllib.error
import urllib.request
from typing import Any

import pytest

from care_logging.logging_config import (
    FILTER_NAME,
    STDERR_HANDLER_NAME,
    apply_split_console_logging,
    build_split_logging_config,
    reset_apply_state,
)

CARE_REF = "develop"
CARE_SETTINGS_FILES = (
    "config/settings/base.py",
    "config/settings/deployment.py",
    "config/settings/test.py",
)
RAW_URL = "https://raw.githubusercontent.com/ohcnetwork/care/{ref}/{path}"


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "care_logging-ci"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8")


def _extract_logging_assignment(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "LOGGING":
                value = ast.literal_eval(node.value)
                if not isinstance(value, dict):
                    raise TypeError("LOGGING assignment was not a dict")
                return value
    raise AssertionError("LOGGING assignment not found in settings module")


def _care_logging_configs() -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    for path in CARE_SETTINGS_FILES:
        url = RAW_URL.format(ref=CARE_REF, path=path)
        source = _fetch_text(url)
        configs[path] = _extract_logging_assignment(source)
    return configs


@pytest.fixture(scope="module")
def care_logging_configs() -> dict[str, dict[str, Any]]:
    try:
        return _care_logging_configs()
    except (urllib.error.URLError, TimeoutError, AssertionError, ValueError, TypeError) as exc:
        pytest.skip(f"Could not fetch Care {CARE_REF} LOGGING configs: {exc}")


@pytest.mark.integration
@pytest.mark.parametrize("settings_path", CARE_SETTINGS_FILES)
def test_latest_care_logging_transform_succeeds(
    care_logging_configs: dict[str, dict[str, Any]],
    settings_path: str,
):
    base = care_logging_configs[settings_path]
    original_disable = base.get("disable_existing_loggers", False)

    config = build_split_logging_config(base)

    assert config["disable_existing_loggers"] is original_disable
    assert FILTER_NAME in config["filters"]
    assert config["handlers"]["console"]["stream"] == "ext://sys.stdout"
    assert FILTER_NAME in config["handlers"]["console"]["filters"]
    assert config["handlers"][STDERR_HANDLER_NAME]["stream"] == "ext://sys.stderr"
    assert STDERR_HANDLER_NAME in config["root"]["handlers"]

    if "time_logging" in base.get("handlers", {}):
        assert config["handlers"]["time_logging"]["stream"] == "ext://sys.stdout"
        assert FILTER_NAME in config["handlers"]["time_logging"]["filters"]
        assert STDERR_HANDLER_NAME in config["loggers"]["time_logging_middleware"]["handlers"]

    for name, logger_cfg in base.get("loggers", {}).items():
        if str(logger_cfg.get("level", "")).upper() != "ERROR":
            continue
        if "console" not in list(logger_cfg.get("handlers") or []):
            continue
        assert config["loggers"][name]["handlers"] == [
            STDERR_HANDLER_NAME if h == "console" else h for h in logger_cfg.get("handlers") or []
        ]
        assert config["loggers"][name]["propagate"] is False


@pytest.mark.integration
@pytest.mark.parametrize("settings_path", CARE_SETTINGS_FILES)
def test_latest_care_logging_routes_streams(
    care_logging_configs: dict[str, dict[str, Any]],
    settings_path: str,
    monkeypatch: pytest.MonkeyPatch,
):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    config = build_split_logging_config(care_logging_configs[settings_path])
    logging.config.dictConfig(config)

    root = logging.getLogger()
    root.info("care-compat-info")
    root.error("care-compat-error")

    assert "care-compat-info" in stdout.getvalue()
    assert "care-compat-error" not in stdout.getvalue()
    assert "care-compat-error" in stderr.getvalue()
    assert "care-compat-info" not in stderr.getvalue()


@pytest.mark.integration
def test_django_loads_plug_against_care_base_logging(
    care_logging_configs: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    """Boot a minimal Django app set with Care base LOGGING + this plug."""
    django = pytest.importorskip("django")
    from django.conf import settings

    if settings.configured:
        pytest.skip("Django settings already configured in this process")

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    reset_apply_state()
    settings.configure(
        SECRET_KEY="care-logging-compat-test",
        INSTALLED_APPS=["care_logging"],
        LOGGING=care_logging_configs["config/settings/base.py"],
        USE_I18N=False,
        USE_TZ=True,
        ROOT_URLCONF=__name__,
    )
    django.setup()

    root = logging.getLogger()
    root.info("django-ready-info")
    root.error("django-ready-error")

    assert "django-ready-info" in stdout.getvalue()
    assert "django-ready-error" in stderr.getvalue()
    assert apply_split_console_logging() is False


urlpatterns: list = []
