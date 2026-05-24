"""Offline smoke tests: argument parsing, config round-trip, error rendering.

These tests do not touch a live backend; live E2E lives in ``cli/tests/
test_cli_live.py`` (skipped unless ``FLOW_CLI_LIVE=1``).
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from flow_cli import __version__
from flow_cli.config import Profile, config_path, load_config, save_config
from flow_cli.credentials import (
    Credential,
    credentials_path,
    delete_credential,
    load_credential,
    save_credential,
)


@pytest.fixture
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FLOW_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_version_flag() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "flow_cli", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert __version__ in result.stdout


def test_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "flow_cli", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "today" in result.stdout
    assert "task" in result.stdout
    assert "note" in result.stdout


def test_status_without_credentials_exits_clean(
    isolated_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "flow_cli", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
        env={**_passthrough_env(monkeypatch), "FLOW_CONFIG_DIR": str(isolated_config_dir)},
    )
    assert result.returncode == 1
    # User-facing message, no Python traceback leak.
    assert "Traceback" not in result.stderr
    assert "not logged in" in result.stderr


def test_config_round_trip(isolated_config_dir: Path) -> None:
    cfg = load_config()
    cfg.profiles["default"] = Profile(
        base_url="https://flow.xeno.garden",
        workspace_id="00000000-0000-0000-0000-000000000001",
        workspace_name="Personal",
    )
    save_config(cfg)
    assert config_path().exists()

    again = load_config()
    assert again.current_profile == "default"
    assert again.profiles["default"].base_url == "https://flow.xeno.garden"
    assert again.profiles["default"].workspace_name == "Personal"


def test_credential_round_trip_and_secure_mode(isolated_config_dir: Path) -> None:
    cred = Credential(
        token="flow_at_test1234",
        token_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        email="me@example.com",
        workspace_id="33333333-3333-3333-3333-333333333333",
        expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
    )
    save_credential("default", cred)
    path = credentials_path()
    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600

    loaded = load_credential("default")
    assert loaded is not None
    assert loaded.token == "flow_at_test1234"
    assert loaded.email == "me@example.com"
    assert loaded.expires_at is not None
    assert loaded.expires_at.year == 2099

    assert delete_credential("default") is True
    assert load_credential("default") is None
    assert delete_credential("default") is False


def _passthrough_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Reuse the parent's env but strip the test runner's pytest plugins
    so the subprocess starts cleanly. Keeps PATH/HOME etc."""
    import os

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env
