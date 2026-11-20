"""Subscription-provider dispatch.

Strix supports running inference on a provider *subscription* instead of a
metered API key. Each provider is a module with a uniform surface
(``subscription_model``, ``is_authenticated``, ``is_content_guardrail_error``,
``PROVIDER`` …). This module routes a ``STRIX_LLM`` value to the matching one so
the rest of the codebase asks "is this a subscription run, and if so which?"
in one place instead of branching on ``codex`` vs ``claude`` everywhere.

Providers:
  - ``chatgpt/<model>`` -> codex (ChatGPT Plus/Pro, OpenAI)         [upstream]
  - ``claude/<model>``  -> claude_oauth (Claude Pro/Max, Anthropic) [scaffold]
"""

from __future__ import annotations

from types import ModuleType

from strix.config import claude_oauth, codex


# Order doesn't matter; prefixes are disjoint.
_PROVIDERS: tuple[ModuleType, ...] = (codex, claude_oauth)

# Human-facing labels for CLI/TUI messaging, keyed by module PROVIDER.
PROVIDER_LABELS: dict[str, str] = {
    codex.PROVIDER: "ChatGPT",
    claude_oauth.PROVIDER: "Claude",
}


def provider_for(model_name: str | None) -> ModuleType | None:
    """The subscription provider module that owns this STRIX_LLM, or None when
    the model is an ordinary (metered API-key) route."""
    for provider in _PROVIDERS:
        if provider.subscription_model(model_name):
            return provider
    return None


def subscription_model(model_name: str | None) -> str | None:
    """The bare model slug when ``model_name`` is any subscription route, else None."""
    provider = provider_for(model_name)
    return provider.subscription_model(model_name) if provider else None


def auth_mode(model_name: str | None) -> str:
    return "subscription" if provider_for(model_name) else "api_key"


def label_for(model_name: str | None) -> str | None:
    """Display label (e.g. "ChatGPT", "Claude") for a subscription route, or None."""
    provider = provider_for(model_name)
    return PROVIDER_LABELS.get(provider.PROVIDER) if provider else None


def is_authenticated_for(model_name: str | None) -> bool:
    """Whether the subscription that ``model_name`` routes to is signed in.

    False for a non-subscription model (there is nothing to be signed into).
    """
    provider = provider_for(model_name)
    return bool(provider and provider.is_authenticated())


def is_content_guardrail_error(exc: BaseException) -> bool:
    """True if any subscription provider recognizes this as a terminal guardrail
    refusal. Cheap to check both — markers are disjoint."""
    return any(provider.is_content_guardrail_error(exc) for provider in _PROVIDERS)
