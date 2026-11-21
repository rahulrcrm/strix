# Scaffold: `claude/` subscription provider

Adds a second subscription path alongside the upstream ChatGPT (Codex) one, so
`STRIX_LLM=claude/<model>` can drive Strix off a **Claude Pro/Max subscription**
via OAuth instead of a metered `LLM_API_KEY`.

**Status (2026-11): working end to end.** The OAuth sign-in, token store, CLI,
routing, **and inference** are all wired and verified against a live subscription
token. `strix auth login claude` signs in; `STRIX_LLM=claude/<model> strix -t …`
runs a real scan driven by the subscription. An Agents-SDK integration test
returns a completion billed to the Claude plan; see "Inference layer" below for
the live findings that settled the implementation.

> **Policy note.** Driving a third-party tool with a Claude Pro/Max *subscription*
> OAuth token is outside Anthropic's consumer / Claude Code terms — the same gray
> area `strix/config/codex.py` openly documents for OpenAI. This fork is for
> private evaluation; use it knowingly. The ToS-clean alternative that works today
> with no fork: `STRIX_LLM=anthropic/claude-<model>` + a metered `LLM_API_KEY`.

## What this branch adds

| File | Change |
| --- | --- |
| `strix/config/claude_oauth.py` | **New.** OAuth 2.0 + PKCE, token store, refresh guard, Anthropic client, `claude/` prefix helpers. Mirrors `codex.py`. |
| `strix/config/subscription.py` | **New.** Dispatcher mapping a `STRIX_LLM` prefix → the owning provider module; one seam the rest of the code queries (`auth_mode`, `label_for`, `is_authenticated_for`, `is_content_guardrail_error`). |
| `strix/config/models.py` | Route `claude/<model>` in `get_model`; guardrail checks go through the dispatcher. `_ClaudeCodeLitellmModel` + `_build_claude_subscription_model` are the inference layer (Bearer auth + system-block gate). |
| `strix/interface/auth_cli.py` | `strix auth login claude` / `status` / `logout claude`. Flow generalized to dispatch on the provider module. |
| `strix/interface/environment.py` | Preflight gate generalized: a `claude/` model requires a Claude sign-in, not an `LLM_API_KEY`. |
| `tests/test_auth_cli.py` | Updated to the multi-provider API + Claude coverage. |

The token store `~/.strix/subscription-auth.json` is **shared** by both
providers, keyed by `PROVIDER` (`"codex"` / `"claude"`). `logout()` only unlinks
the file when the dict is empty, so signing out of one leaves the other intact.

## OAuth constants (sourced)

Filled in from the Claude Code CLI bundle + a working PKCE implementation (2026-11):

| Value | Setting |
| --- | --- |
| `CLIENT_ID` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` (Claude Code's public client) |
| Authorize | `https://claude.ai/oauth/authorize` (Pro/Max subscription) + `code=true` |
| Token | `https://platform.claude.com/v1/oauth/token` (alt: `console.anthropic.com`) |
| Redirect | `https://platform.claude.com/oauth/code/callback` (manual copy-paste) |
| Scope | `user:inference user:profile user:sessions:claude_code user:mcp_servers` |
| Flow | manual paste (`USES_LOOPBACK_SERVER = False`), token body form-urlencoded, echoes `state` |
| Beta header | `anthropic-beta: oauth-2025-04-20` — VERIFIED; the call also works without it, kept to match real Claude Code |

**Version drift:** Claude Code moved `console.anthropic.com` → `platform.claude.com`
across versions. If a token exchange 401/404s, flip `TOKEN_URL` and `REDIRECT_URI`
to the documented `console.anthropic.com` alternates in `claude_oauth.py` (keep
them identical to each other). The redirect URI must match the client's
registration exactly.

## Inference layer (IMPLEMENTED + VERIFIED, 2026-11)

Settled against a live subscription token via `/v1/messages` probes. Implemented
in `models.py` as `_ClaudeCodeLitellmModel` (a `LitellmModel` subclass) + routing
through the shared stream-timeout / per-turn guard.

1. **Auth → path A (LiteLLM).** `Authorization: Bearer <token>` is accepted.
   LiteLLM's Anthropic path also emits an `x-api-key` from `api_key`; a live check
   confirmed a **placeholder** `x-api-key` alongside the Bearer header does **not**
   get the request rejected — so no header stripping and no native-client path B
   is needed. The subclass injects `claude_oauth.oauth_extra_headers()` (fresh
   token per attempt, so long scans survive expiry) via `model_settings.extra_headers`.

2. **System-prompt gate — the load-bearing finding.** The OAuth token is accepted
   **only** when the request `system` is an **array whose FIRST block is EXACTLY**
   `claude_oauth.CLAUDE_CODE_SYSTEM_PREAMBLE`
   (`"You are Claude Code, Anthropic's official CLI for Claude."`). Any other first
   block — a different prompt, or the preamble with *anything* appended **in the
   same block** — is rejected with a **disguised `HTTP 429 rate_limit_error`**
   (not a 401/403, and not real throttling: a known-good control succeeds between
   two such rejections). Additional system content is fine in **later** blocks.

   So `_ClaudeCodeLitellmModel._fetch_response` moves Strix's own system prompt
   into a *second* leading `role:"system"` message and passes the preamble as
   `system_instructions`. LiteLLM's `translate_system_message` emits one Anthropic
   system block per leading system message (`split_leading_system_run`), yielding
   `system=[preamble, strix-prompt]`. This is done in the model, **not** in
   `_split_cached_prefix`.

   > Prompt caching is currently **not** applied on this route (the CACHE_POINT /
   > `cache_control` split is bypassed). Correctness first; re-adding cache blocks
   > behind the preamble is a follow-up to cut subscription token use.

3. **Content guardrail** (still a stub) — the pentest system prompt used in the
   integration test was accepted, but a full autonomous scan may still draw a
   content refusal the way ChatGPT's guardrail can. `ClaudeContentGuardrailError`
   and its markers in `claude_oauth.py` remain stubs; confirm the real refusal
   shape against a live rejection, then wire detection in `core/execution.py`
   (mirrors codex).

## Remaining seams (reporting/telemetry — not blocking)

The upstream Codex path also touches `interface/utils.py`, `scan_setup.py`,
`report/state.py`, `report/usage.py`, `report/pricing.py`, telemetry, and the
TUI. These query `codex.auth_mode` / `codex.subscription_model` directly for
display and budget. Point them at `strix.config.subscription` (the dispatcher) so
`claude/` runs show the right label and budget logic treats a subscription run as
unmetered. Enumerate them with:

```bash
grep -rn "codex\.\|subscription\|auth_mode" strix --include="*.py"
```

## Run it

`uv` wasn't available in the build environment, so a plain venv works (Python
3.12+; verified on 3.14):

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest pytest-asyncio
.venv/bin/strix auth login claude          # one-time OAuth sign-in (manual paste)
STRIX_LLM=claude/claude-sonnet-4-5 .venv/bin/strix -n -t https://example.com --scan-mode quick --max-turns 40
```

Tests (passing 2026-11: auth_cli 18, models + codex 69):

```bash
.venv/bin/python -m pytest tests/test_auth_cli.py tests/test_models.py tests/test_codex_auth.py tests/test_codex_streaming.py -q
```
