"""Claude (Anthropic) subscription auth: OAuth login, token refresh, and the
Anthropic client that routes inference through a Claude Pro/Max subscription.

Mirrors the Codex path (see ``strix/config/codex.py``): OAuth 2.0 + PKCE, with
the access token sent as a ``Bearer`` token (not an ``x-api-key``) to the
Anthropic Messages API, plus the ``anthropic-beta: oauth-2025-04-20`` header.

SCAFFOLD STATUS
---------------
The OAuth flow (login → token → refresh) is wired with values sourced from the
Claude Code CLI bundle and a working PKCE implementation (see SOURCES below).
Unlike codex.py's localhost loopback, Claude Code uses a MANUAL copy-paste flow:
the redirect is a hosted callback page that displays ``code#state`` for the user
to paste back. ``USES_LOOPBACK_SERVER = False`` makes auth_cli skip the local
HTTP server for this provider.

Two things still need a LIVE token to settle (the inference layer, not auth):
  1. Whether a ``Bearer`` OAuth request to ``/v1/messages`` succeeds with Strix's
     own system prompt, or is gated on a Claude Code identity preamble. If gated,
     inject it in ``models.py`` ``_split_cached_prefix``, not here.
  2. The ``anthropic-beta`` header required for OAuth inference. ``OAUTH_BETA``
     below (``oauth-2025-04-20``) is the community value but appears in NONE of
     the sourced artifacts — confirm against a live call before relying on it.
Also: ``ClaudeContentGuardrailError`` markers are stubs (confirm a real refusal).

SOURCES (2026-11, may drift — Claude Code moved console.anthropic.com →
platform.claude.com across versions; keep the alternates below in mind):
  - cli.js v2.0.69 extraction: client_id, authorize/token/callback hosts.
  - platform.claude.com/docs OAuth reference: PKCE S256, redirect-URI rules.
  - Working manual-PKCE implementation: authorize params (incl. ``code=true``),
    form-urlencoded token body incl. ``state``, scope string.

POLICY
------
Driving a third-party tool with a Claude Pro/Max *subscription* OAuth token is
outside Anthropic's consumer / Claude Code terms, the same way codex.py notes for
OpenAI. This fork is for private, internal evaluation; the user chooses this path
knowingly. The ToS-clean alternative is ``STRIX_LLM=anthropic/claude-...`` with a
metered ``LLM_API_KEY`` (already supported upstream, no OAuth).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from strix.utils.secret_files import write_secret_text


if TYPE_CHECKING:
    from collections.abc import Iterator

    from anthropic import AsyncAnthropic


logger = logging.getLogger(__name__)


PROVIDER = "claude"

# OAuth client of the Claude Code CLI (sourced — see module docstring SOURCES).
# This is Claude Code's own public client id, reused so a Pro/Max subscription
# sign-in works; it is not a secret.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # noqa: S105 - public client id

# Authorize on claude.ai for a Pro/Max *subscription* sign-in (console.anthropic
# .com/oauth/authorize is the Console/API-billing variant of the same client).
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"

# Token + redirect hosts moved console.anthropic.com -> platform.claude.com
# across Claude Code versions. Newest extraction uses platform.claude.com; if a
# token exchange 404s/401s, try the console.anthropic.com alternates below.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 - URL, not a secret
# Alt (v2.0.69): "https://console.anthropic.com/v1/oauth/token"

# Manual copy-paste callback: a hosted page that shows ``code#state`` to paste
# back. MUST exactly match the redirect_uri registered for CLIENT_ID and be
# identical in the authorize request and the token exchange.
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
# Alt (v2.0.69): "https://console.anthropic.com/oauth/code/callback"

# Sourced scope set for subscription inference (order matches the authorize URL).
SCOPE = "user:inference user:profile user:sessions:claude_code user:mcp_servers"

# Claude Code uses a hosted callback + manual paste, not a localhost listener.
# auth_cli reads this to skip starting a loopback server for this provider.
USES_LOOPBACK_SERVER = False
# The token exchange must echo the locally-generated ``state``; auth_cli reads
# this to pass it through ``exchange_code``.
EXCHANGE_NEEDS_STATE = True

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
# SCAFFOLD/UNVERIFIED: community value for the OAuth-inference beta header; it is
# in none of the sourced artifacts. Confirm against a live /v1/messages call.
OAUTH_BETA = "oauth-2025-04-20"

_TOKEN_TIMEOUT = 30
_EXPIRY_SKEW_S = 300

_refresh_lock = threading.Lock()

# Shared with the Codex path: one store keyed by provider, so both subscriptions
# can coexist. Keep the "delete only when the dict is empty" guard in logout().
AUTH_PATH = Path.home() / ".strix" / "subscription-auth.json"


def _read_store() -> dict[str, Any]:
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(data: dict[str, Any]) -> None:
    write_secret_text(AUTH_PATH, json.dumps(data, indent=2))


def read_record() -> dict[str, Any] | None:
    record = _read_store().get(PROVIDER)
    if not isinstance(record, dict) or record.get("type") != "oauth":
        return None
    if not (record.get("access") and record.get("refresh")):
        return None
    return record


def is_authenticated() -> bool:
    return read_record() is not None


def save_record(record: dict[str, Any]) -> None:
    data = _read_store()
    data[PROVIDER] = record
    _write_store(data)


def logout() -> None:
    data = _read_store()
    if PROVIDER not in data:
        return
    del data[PROVIDER]
    # Shared store: only unlink when nothing (e.g. the codex record) is left.
    if data:
        _write_store(data)
        return
    with contextlib.suppress(OSError):
        AUTH_PATH.unlink()


@contextlib.contextmanager
def _refresh_guard() -> Iterator[None]:
    """Serialize token refresh within (lock) and across (flock) Strix processes,
    so concurrent runs can't both spend the single-use refresh token."""
    with _refresh_lock:
        try:
            import fcntl

            lock_path = AUTH_PATH.with_suffix(".claude.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("w")
        except (ImportError, OSError):
            yield
            return
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class ClaudeAuthError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class ClaudeContentGuardrailError(Exception):
    """The Anthropic backend refused a request via a content guardrail.
    Terminal — retrying identical content never clears the block.

    SCAFFOLD: markers below are a guess. Confirm the real refusal shape against a
    live rejection before relying on this, then wire detection in
    ``core/execution.py`` and ``config/models.py`` (mirror the codex seam).
    """

    def __init__(self, model: str, original: BaseException | None = None) -> None:
        self.model = model
        self.original = original
        super().__init__(
            f"'{model}' was blocked by Anthropic's content guardrails. "
            f"Set STRIX_LLM to a model/path that isn't blocked and re-run."
        )


_GUARDRAIL_MARKERS = (
    # VERIFY against a real rejection body; placeholders only.
    "blocked by content policy",
    "usage policy",
)


def is_content_guardrail_error(exc: BaseException) -> bool:
    if isinstance(exc, ClaudeContentGuardrailError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _GUARDRAIL_MARKERS)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    # 32 random bytes -> 43-char base64url verifier (matches Claude Code; the
    # RFC 7636 range is 43-128 chars). challenge = base64url(SHA256(verifier)).
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def create_state() -> str:
    return _b64url(secrets.token_bytes(32))


def build_authorize_url(challenge: str, state: str) -> str:
    # Param set + the ``code=true`` flag match Claude Code's own authorize URL.
    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_redirect_input(value: str) -> tuple[str | None, str | None]:
    """Extract ``(code, state)`` from a pasted redirect URL, ``code#state``,
    query string, or bare code."""
    value = (value or "").strip()
    if not value:
        return None, None
    with contextlib.suppress(ValueError):
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme and parsed.query:
            query = urllib.parse.parse_qs(parsed.query)
            return _first(query, "code"), _first(query, "state")
    if "#" in value:
        code, _, state = value.partition("#")
        return code or None, state or None
    if "code=" in value:
        query = urllib.parse.parse_qs(value)
        return _first(query, "code"), _first(query, "state")
    return value, None


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _post_form(payload: dict[str, str]) -> dict[str, Any]:
    detail = ""
    try:
        with requests.post(
            TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json"},
            timeout=_TOKEN_TIMEOUT,
        ) as response:
            status_code = response.status_code
            body = response.content
            if status_code >= 400:
                detail = response.text[:300]
    except requests.RequestException as exc:
        raise ClaudeAuthError("unavailable", str(exc)) from exc
    if status_code >= 400:
        raise ClaudeAuthError("token_http_error", f"HTTP {status_code}: {detail}")
    data = json.loads(body or b"{}")
    if not isinstance(data, dict):
        raise ClaudeAuthError("bad_response", "token endpoint returned non-object")
    return data


def _record_from_token_response(
    data: dict[str, Any], refresh_fallback: str | None = None
) -> dict[str, Any]:
    access = data.get("access_token")
    # A refresh response may omit refresh_token when it isn't rotated; keep the old one.
    refresh = data.get("refresh_token") or refresh_fallback
    expires_in = data.get("expires_in")
    if not isinstance(access, str) or not access:
        raise ClaudeAuthError("bad_response", "token response missing access_token")
    if not isinstance(refresh, str) or not refresh:
        raise ClaudeAuthError("bad_response", "token response missing refresh_token")
    ttl = expires_in if isinstance(expires_in, int | float) else 3600
    return {
        "type": "oauth",
        "provider": PROVIDER,
        "access": access,
        "refresh": refresh,
        "expires_at": time.time() + ttl,
    }


def exchange_code(code: str, verifier: str, state: str | None = None) -> dict[str, Any]:
    # Claude Code splits the pasted ``code#state`` and sends only the part before
    # ``#`` as ``code``, plus the locally generated ``state`` (passed in here).
    # Body is form-urlencoded (JSON reportedly times out on this endpoint).
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code.split("#", 1)[0],
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
    if state is not None:
        payload["state"] = state
    return _record_from_token_response(_post_form(payload))


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    data = _post_form(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
    )
    return _record_from_token_response(data, refresh_fallback=refresh_token)


def _near_expiry(record: dict[str, Any]) -> bool:
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, int | float):
        return True
    return expires_at - _EXPIRY_SKEW_S <= time.time()


def get_valid_token() -> str:
    """Return a valid access token, refreshing under the cross-process guard if
    near expiry."""
    record = read_record()
    if record is None:
        raise ClaudeAuthError("not_authenticated", "not signed in; run: strix auth login claude")
    if not _near_expiry(record):
        return record["access"]
    with _refresh_guard():
        record = read_record()
        if record is None:
            raise ClaudeAuthError(
                "not_authenticated", "not signed in; run: strix auth login claude"
            )
        if not _near_expiry(record):
            return record["access"]
        try:
            refreshed = refresh_tokens(record["refresh"])
        except ClaudeAuthError:
            # A peer process may have already spent this single-use refresh token.
            latest = read_record()
            if latest and latest["refresh"] != record["refresh"] and not _near_expiry(latest):
                return latest["access"]
            raise
        save_record(refreshed)
        return refreshed["access"]


def build_anthropic_client() -> AsyncAnthropic:
    """An ``AsyncAnthropic`` bound to the subscription OAuth token.

    ``AsyncAnthropic(auth_token=...)`` sends ``Authorization: Bearer`` natively
    (``api_key=`` would send ``x-api-key``). A per-request hook re-stamps a fresh
    token so long scans survive expiry, and the OAuth beta header is attached.

    SCAFFOLD: whether the Agents SDK can drive this client directly, or whether
    the LiteLLM route with OAuth ``extra_headers`` is enough, is the open
    architecture question (see models.py). Keep this as the fallback client for
    the hand-rolled ``Model`` path.
    """
    import asyncio

    import httpx
    from anthropic import AsyncAnthropic

    from strix.llm import request_log

    get_valid_token()  # fail fast at configure time if the sign-in is dead

    async def _auth_hook(request: httpx.Request) -> None:
        access = await asyncio.to_thread(get_valid_token)
        request.headers["Authorization"] = f"Bearer {access}"
        # Ensure no stale x-api-key rides along with the OAuth token.
        if "x-api-key" in request.headers:
            del request.headers["x-api-key"]

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=30.0),
        event_hooks={"request": [_auth_hook]},
    )
    request_log.observe_http_client(http_client)
    return AsyncAnthropic(
        auth_token="strix-claude-oauth",  # placeholder; the hook overwrites Authorization
        base_url=ANTHROPIC_BASE_URL,
        http_client=http_client,
        default_headers={"anthropic-beta": OAUTH_BETA},
    )


def oauth_extra_headers() -> dict[str, str]:
    """Headers for the LiteLLM route: Bearer OAuth token + the oauth beta flag.

    SCAFFOLD: LiteLLM's Anthropic path also emits ``x-api-key`` from ``api_key``.
    Verify a stray ``x-api-key`` does not ride along and get rejected; if it does,
    use ``build_anthropic_client`` with a hand-rolled ``Model`` instead.
    """
    return {
        "authorization": f"Bearer {get_valid_token()}",
        "anthropic-beta": OAUTH_BETA,
    }


_subscription_client: AsyncAnthropic | None = None


def get_subscription_client() -> AsyncAnthropic:
    global _subscription_client  # noqa: PLW0603
    if _subscription_client is None:
        _subscription_client = build_anthropic_client()
    return _subscription_client


SUBSCRIPTION_PREFIX = "claude/"


def subscription_model(model_name: str | None) -> str | None:
    """The model slug behind a ``claude/<model>`` STRIX_LLM, or None.

    e.g. ``claude/claude-opus-4-5`` -> ``anthropic/claude-opus-4-5`` for LiteLLM,
    or ``claude-opus-4-5`` for the native Anthropic client. Returns the raw slug;
    the caller decides the provider prefix.
    """
    name = (model_name or "").strip()
    if not name.lower().startswith(SUBSCRIPTION_PREFIX):
        return None
    return name[len(SUBSCRIPTION_PREFIX) :] or None


def auth_mode(model_name: str | None) -> str:
    return "subscription" if subscription_model(model_name) else "api_key"
