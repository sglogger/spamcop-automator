from __future__ import annotations

import pytest

from spamcop_automator import cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SPAMCOP_USERNAME", raising=False)
    monkeypatch.delenv("SPAMCOP_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", tmp_path / "missing.ini")


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SPAMCOP_USERNAME", "me")
    monkeypatch.setenv("SPAMCOP_PASSWORD", "pw")
    assert cli.load_credentials(None) == ("me", "pw")


def test_credentials_from_file(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[spamcop]\nusername = me\npassword = p%w d\n")
    path.chmod(0o600)
    assert cli.load_credentials(path) == ("me", "p%w d")


def test_missing_credentials():
    with pytest.raises(cli.ConfigError, match="No credentials"):
        cli.load_credentials(None)


def test_missing_explicit_config(tmp_path):
    with pytest.raises(cli.ConfigError, match="not found"):
        cli.load_credentials(tmp_path / "nope.ini")


def test_main_without_credentials_exits_2():
    assert cli.main([]) == cli.EXIT_CONFIG
