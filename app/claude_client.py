"""Anthropic client + model constants. Deliberately no provider abstraction —
swapping models is a one-line change, so it's not worth engineering around."""

from __future__ import annotations

import os
import time
from typing import Callable, TypeVar

import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

DASHBOARD_MODEL = "claude-opus-4-8"
CHAT_MODEL = "claude-opus-4-8"

# Per-million-token pricing, USD — used only to estimate spend for the budget
# cutoff in rate_limit.py. These are placeholder Opus-tier defaults; set the
# real numbers for your model from https://www.anthropic.com/pricing via
# ANTHROPIC_PRICE_INPUT_PER_MTOK / ANTHROPIC_PRICE_OUTPUT_PER_MTOK in .env —
# the defaults deliberately skew high so an unconfigured deployment is
# cautious rather than blind to real spend.
PRICE_INPUT_PER_MTOK = float(os.environ.get("ANTHROPIC_PRICE_INPUT_PER_MTOK", "15"))
PRICE_OUTPUT_PER_MTOK = float(os.environ.get("ANTHROPIC_PRICE_OUTPUT_PER_MTOK", "75"))


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * PRICE_INPUT_PER_MTOK + (
        output_tokens / 1_000_000
    ) * PRICE_OUTPUT_PER_MTOK


_client: Anthropic | None = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        _client = Anthropic(api_key=api_key)
    return _client


class ClaudeUnavailableError(Exception):
    """The API is transiently unavailable (overloaded, rate-limited, network
    blip) — worth telling the user to just retry in a moment."""


class ClaudeRequestError(Exception):
    """Something about the request itself is wrong (bad input, auth, payload
    too large) — retrying won't help."""


# The Anthropic SDK already retries connection errors / 429 / 5xx internally
# (default max_retries=2 with backoff). This wraps the *whole* call one more
# time for the cases that can still outlast that budget in practice — e.g. a
# sustained "overloaded" window — before giving up and surfacing a clean error
# instead of letting a raw SDK exception become an unhandled 500.
_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
)

T = TypeVar("T")


def call_with_retry(fn: Callable[[], T], attempts: int = 2, base_delay: float = 1.5) -> T:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
        except anthropic.AnthropicError as exc:
            raise ClaudeRequestError(str(exc)) from exc

    raise ClaudeUnavailableError(str(last_exc)) from last_exc
