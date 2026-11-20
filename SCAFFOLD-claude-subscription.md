# Scaffold: `claude/` subscription provider

Adds a second subscription path alongside the upstream ChatGPT (Codex) one, so
`STRIX_LLM=claude/<model>` can drive Strix off a **Claude Pro/Max subscription**
via OAuth instead of a metered `LLM_API_KEY`.

This is a **scaffold**: the auth flow, token store, CLI, and routing are wired;
the inference call and a few security-critical constants are intentionally left
as verified-before-use integration points (see below). It does **not** run
end-to-end yet.

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
| `strix/config/models.py` | Route `claude/<model>` in `get_model`; guardrail checks go through the dispatcher. `_build_claude_subscription_model` is the inference boundary. |
| `strix/interface/auth_cli.py` | `strix auth login claude` / `status` / `logout claude`. Flow generalized to dispatch on the provider module. |
| `strix/interface/environment.py` | Preflight gate generalized: a `claude/` model requires a Claude sign-in, not an `LLM_API_KEY`. |
| `tests/test_auth_cli.py` | Updated to the multi-provider API + Claude coverage. |

The token store `~/.strix/subscription-auth.json` is **shared** by both
providers, keyed by `PROVIDER` (`"codex"` / `"claude"`). `logout()` only unlinks
the file when the dict is empty, so signing out of one leaves the other intact.

## Must verify before it works (needs a live token)

1. **OAuth constants** — `CLIENT_ID`, `REDIRECT_URI` (host/port/path) and `SCOPE`
   in `claude_oauth.py` are placeholders. `CLIENT_ID` is literally
   `VERIFY_CLAUDE_CODE_OAUTH_CLIENT_ID`; the routing refuses to run until it's
   replaced. The redirect URI must match the client's server-side registration
   exactly — you cannot pick the port freely. **Source these from a primary
   artifact (Claude Code's distributed bundle or a maintained OSS implementation
   of this flow); do not recall them.**

2. **Auth header path** — OAuth needs `Authorization: Bearer <token>` +
   `anthropic-beta: oauth-2025-04-20`, *not* `x-api-key`. Decide the model layer
   with one standalone check:

   ```bash
   python -c "import litellm; print(litellm.completion(
     model='anthropic/claude-<model>',
     messages=[{'role':'user','content':'hi'}],
     extra_headers={'authorization':'Bearer <token>','anthropic-beta':'oauth-2025-04-20'},
     max_tokens=16))"
   ```

   - If LiteLLM does **not** also emit a conflicting `x-api-key` → **path A**:
     reuse `LitellmModel` and inject `claude_oauth.oauth_extra_headers()`. ~minimal.
   - If it does and Anthropic rejects the pair → **path B**: drive
     `claude_oauth.get_subscription_client()` (`AsyncAnthropic(auth_token=…)`
     sends Bearer natively) from a hand-rolled `Model` subclass, analogous to
     `_CodexResponsesModel`. Heavier.

   Both paths are sketched in `_build_claude_subscription_model` in `models.py`.

3. **System-prompt gating** — confirm with one curl whether `/v1/messages`
   accepts an OAuth token with an arbitrary system prompt, or requires the system
   message to begin with the Claude Code identity string. If gated, inject the
   preamble in `_split_cached_prefix` (`models.py`), **not** in the OAuth module —
   Strix sends its own large pentest system prompt.

4. **Content guardrail** (second-order) — Anthropic may refuse pentest-shaped
   content the way ChatGPT's guardrail does. `ClaudeContentGuardrailError` and its
   markers in `claude_oauth.py` are stubs; confirm the real refusal shape against
   a live rejection, then wire detection in `core/execution.py` (mirrors codex).

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

## Run the tests

Not run in the scaffolding environment (deps weren't installable there). Run:

```bash
make dev-install
uv run pytest tests/test_auth_cli.py tests/test_models.py tests/test_usage_subscription.py
make check-all
```
