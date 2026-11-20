"""Tests for the `strix auth` CLI: subcommand routing and provider dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from strix.config import claude_oauth, codex
from strix.interface import auth_cli


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "home" / ".strix" / "subscription-auth.json"
    # Both providers share one store; point both at the temp path.
    monkeypatch.setattr(codex, "AUTH_PATH", store)
    monkeypatch.setattr(claude_oauth, "AUTH_PATH", store)


def test_default_login_provider_is_chatgpt() -> None:
    assert auth_cli.DEFAULT_LOGIN_PROVIDER == "chatgpt"
    assert auth_cli._resolve_provider("chatgpt") is codex
    assert auth_cli._resolve_provider("codex") is codex
    assert auth_cli._resolve_provider("claude") is claude_oauth
    assert auth_cli._resolve_provider("anthropic") is claude_oauth
    assert auth_cli._resolve_provider("gemini") is None


def test_unknown_subcommand_returns_usage_error() -> None:
    assert auth_cli.run_auth(["bogus"]) == 2


def test_help_returns_zero() -> None:
    assert auth_cli.run_auth(["--help"]) == 0


def test_status_not_signed_in() -> None:
    assert auth_cli.run_auth(["status"]) == 1


def test_login_rejects_unsupported_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def _should_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        msg = "OAuth flow must not start for an unsupported provider"
        raise AssertionError(msg)

    monkeypatch.setattr(auth_cli, "_run_oauth_flow", _should_not_run)
    assert auth_cli.run_auth(["login", "gemini"]) == 2


def test_finish_requires_state_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex, "exchange_code", lambda *_: {"ok": True})

    # Loopback (require_state=True): missing or mismatched state is rejected.
    with pytest.raises(codex.CodexAuthError) as missing:
        auth_cli._finish(codex, "code", None, "verifier", "expected", require_state=True)
    assert missing.value.code == "state_mismatch"
    with pytest.raises(codex.CodexAuthError) as mismatch:
        auth_cli._finish(codex, "code", "wrong", "verifier", "expected", require_state=True)
    assert mismatch.value.code == "state_mismatch"

    # Matching state proceeds to the exchange.
    assert auth_cli._finish(
        codex, "code", "expected", "verifier", "expected", require_state=True
    ) == {"ok": True}


def test_finish_manual_paste_allows_absent_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex, "exchange_code", lambda *_: {"ok": True})
    # Manual paste (require_state=False): a bare code with no state is accepted,
    # but a present-and-wrong state is still rejected.
    assert auth_cli._finish(
        codex, "code", None, "verifier", "expected", require_state=False
    ) == {"ok": True}
    with pytest.raises(codex.CodexAuthError):
        auth_cli._finish(codex, "code", "wrong", "verifier", "expected", require_state=False)


def test_finish_rejects_missing_code() -> None:
    with pytest.raises(codex.CodexAuthError) as exc:
        auth_cli._finish(codex, None, "expected", "verifier", "expected", require_state=True)
    assert exc.value.code == "no_code"


def test_finish_uses_claude_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # CSRF validation holds for the claude provider too, raising ITS error class.
    with pytest.raises(claude_oauth.ClaudeAuthError) as exc:
        auth_cli._finish(claude_oauth, "code", None, "verifier", "expected", require_state=True)
    assert exc.value.code == "state_mismatch"


def test_model_subcommand_removed() -> None:
    assert auth_cli.run_auth(["model", "gpt-5.5"]) == 2


@pytest.mark.parametrize(
    ("provider_name", "provider_mod"),
    [
        ("chatgpt", codex),
        ("codex", codex),
        ("ChatGPT", codex),
        ("claude", claude_oauth),
        ("anthropic", claude_oauth),
    ],
)
def test_login_accepts_provider_aliases(
    provider_name: str, provider_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached = {"flow": False}

    def _fake_flow(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        reached["flow"] = True
        return {
            "type": "oauth",
            "provider": provider_mod.PROVIDER,
            "access": "a",
            "refresh": "r",
            "expires_at": 0,
        }

    monkeypatch.setattr(auth_cli, "_run_oauth_flow", _fake_flow)
    monkeypatch.setattr(provider_mod, "save_record", lambda _record: None)

    assert auth_cli.run_auth(["login", provider_name]) == 0
    assert reached["flow"] is True


def test_logout_specific_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(codex, "logout", lambda: calls.append("codex"))
    monkeypatch.setattr(claude_oauth, "logout", lambda: calls.append("claude"))

    assert auth_cli.run_auth(["logout", "claude"]) == 0
    assert calls == ["claude"]


def test_logout_all_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(codex, "logout", lambda: calls.append("codex"))
    monkeypatch.setattr(claude_oauth, "logout", lambda: calls.append("claude"))

    assert auth_cli.run_auth(["logout"]) == 0
    assert set(calls) == {"codex", "claude"}
