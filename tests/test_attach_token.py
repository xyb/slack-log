"""Unit tests for attach.load_token — env var precedence + .env file fallback.

Token resolution order (highest priority first):
  1. SLACK_XOXC + SLACK_XOXD environment variables (both required as a pair)
  2. ./.env in the current working directory
  3. ~/.config/slack-log/.env (XDG-respecting)
  4. RuntimeError

The .env file format follows the de-facto standard parsed by python-dotenv.
"""

from pathlib import Path

import pytest

from slack_log import attach


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Don't let the caller's real env leak into tests."""
    monkeypatch.delenv("SLACK_XOXC", raising=False)
    monkeypatch.delenv("SLACK_XOXD", raising=False)


def test_env_vars_take_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no cwd .env interferes
    monkeypatch.setattr(attach, "USER_DOTENV", tmp_path / "absent.env")
    monkeypatch.setenv("SLACK_XOXC", "xoxc-from-env")
    monkeypatch.setenv("SLACK_XOXD", "xoxd-from-env")
    assert attach.load_token() == ("xoxc-from-env", "xoxd-from-env")


def test_cwd_dotenv_used_when_env_missing(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(attach, "USER_DOTENV", tmp_path / "absent.env")
    (tmp_path / ".env").write_text(
        "# slack-log creds\n"
        "SLACK_XOXC=xoxc-from-cwd\n"
        "SLACK_XOXD=xoxd-from-cwd\n"
    )
    assert attach.load_token() == ("xoxc-from-cwd", "xoxd-from-cwd")


def test_user_dotenv_used_when_no_cwd_and_no_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    user_env = tmp_path / "user" / ".env"
    user_env.parent.mkdir()
    user_env.write_text("SLACK_XOXC=xoxc-user\nSLACK_XOXD=xoxd-user\n")
    monkeypatch.setattr(attach, "USER_DOTENV", user_env)
    assert attach.load_token() == ("xoxc-user", "xoxd-user")


def test_cwd_dotenv_overrides_user_dotenv(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    user_env = tmp_path / "user" / ".env"
    user_env.parent.mkdir()
    user_env.write_text("SLACK_XOXC=xoxc-user\nSLACK_XOXD=xoxd-user\n")
    monkeypatch.setattr(attach, "USER_DOTENV", user_env)
    (tmp_path / ".env").write_text("SLACK_XOXC=xoxc-cwd\nSLACK_XOXD=xoxd-cwd\n")
    # cwd .env wins over ~/.config/slack-log/.env
    assert attach.load_token() == ("xoxc-cwd", "xoxd-cwd")


def test_dotenv_handles_quotes(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(attach, "USER_DOTENV", tmp_path / "absent.env")
    (tmp_path / ".env").write_text('SLACK_XOXC="xoxc-quoted"\nSLACK_XOXD=\'xoxd-quoted\'\n')
    assert attach.load_token() == ("xoxc-quoted", "xoxd-quoted")


def test_partial_env_falls_back_to_file(tmp_path: Path, monkeypatch):
    """Only one of two env vars set ⇒ fall back to file for both."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(attach, "USER_DOTENV", tmp_path / "absent.env")
    monkeypatch.setenv("SLACK_XOXC", "xoxc-from-env")  # SLACK_XOXD not set
    (tmp_path / ".env").write_text("SLACK_XOXC=xoxc-file\nSLACK_XOXD=xoxd-file\n")
    assert attach.load_token() == ("xoxc-file", "xoxd-file")


def test_missing_credentials_raises(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no cwd .env
    monkeypatch.setattr(attach, "USER_DOTENV", tmp_path / "absent.env")
    with pytest.raises(RuntimeError, match="Slack credentials"):
        attach.load_token()


def test_dotenv_missing_one_key_raises(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(attach, "USER_DOTENV", tmp_path / "absent.env")
    (tmp_path / ".env").write_text("SLACK_XOXC=only-xoxc-here\n")  # no SLACK_XOXD
    with pytest.raises(RuntimeError, match="Slack credentials"):
        attach.load_token()
