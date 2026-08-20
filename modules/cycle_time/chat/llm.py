"""
llm.py  (cycle_time.chat)
─────────────────────────
The one place that decides WHICH model answers — and what happens when it
cannot. Everything else (agent, router, sqllane) imports this and never knows
the difference.

PRIMARY + FALLBACK, NOT PRIMARY OR NOTHING
  CHAT_PROVIDER=openai puts an OpenAI-compatible cloud model (Groq
  llama-3.3-70b by default) in front, with the local Ollama 8B as automatic
  fallback: a rate-limit, an outage or a missing key degrades to slower-but-
  real answers, never to an error bubble. Every payload reports the model that
  ACTUALLY answered (answered_by), because a 70B answer and an 8B answer are
  not the same thing and the UI already shows the label.

  Fallback is one-directional and per-call. There is no retry storm: one cloud
  attempt (openai_compat itself retries only the structured-output MODE), then
  local.

WHY A CONTEXTVAR FOR answered_by
  ask() runs in FastAPI's threadpool — a module global would race across
  concurrent users. A ContextVar is per-thread, so each request tracks its own
  answering model.

Company policy is currently open for AI experimentation, so the provider is an
env decision: the lanes, the cage, the semantic layer and the eval all carry
over unchanged. `python -m modules.cycle_time.chat.eval` is the acceptance
test for any new provider: 31/31 or it does not ship.
"""

from __future__ import annotations

import contextvars
import logging

from modules.cycle_time.config import CHAT_MODEL, CHAT_PROVIDER, OLLAMA_MODEL
from modules.cycle_time.chat import ollama as _local
from modules.cycle_time.chat.ollama import OllamaError as LLMError  # one error type everywhere

log = logging.getLogger(__name__)

_CLOUD = CHAT_PROVIDER == "openai"

#: The configured PRIMARY. answered_by() tells you who actually spoke.
MODEL: str = CHAT_MODEL if _CLOUD else OLLAMA_MODEL

_answered: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "chat_answered_by", default=None)


def rich() -> bool:
    """True when the configured primary is a cloud-class model — the compose
    muzzle (one short sentence, tiny payloads) exists for the local 8B and
    comes OFF for a big brain. Checked at question time, config-only."""
    return _cloud_ready()


def answered_by() -> str | None:
    """The model that served the most recent call on THIS request thread."""
    return _answered.get()


def note_answered(name: str) -> None:
    """For callers that talk to a backend directly (the rich agent loop) so
    the payload's model label stays truthful."""
    _answered.set(name)


def reset_answered() -> None:
    _answered.set(None)


def _cloud_ready() -> bool:
    if not _CLOUD:
        return False
    from modules.cycle_time.chat import openai_compat
    return openai_compat.available()[0]


def available() -> tuple[bool, str]:
    """(usable, detail). With a fallback in the chain, "usable" means AT LEAST
    ONE backend answers — a dead cloud with a live local card is still a
    working chat."""
    if _CLOUD:
        from modules.cycle_time.chat import openai_compat
        ok, detail = openai_compat.available()
        if ok:
            return True, f"{CHAT_MODEL} (local fallback: {OLLAMA_MODEL})"
        lok, ldetail = _local.available()
        if lok:
            return True, f"local {OLLAMA_MODEL} ({detail})"
        return False, f"{detail}; local also unavailable: {ldetail}"
    return _local.available()


def chat(messages: list[dict], tools=None, temperature: float = 0.0,
         format: dict | str | None = None) -> dict:
    if _cloud_ready():
        from modules.cycle_time.chat import openai_compat
        try:
            msg = openai_compat.chat(messages, tools, temperature, format)
            _answered.set(CHAT_MODEL)
            return msg
        except LLMError as e:
            log.warning("chat cloud failed (%s) — falling back to local", e)
    msg = _local.chat(messages, tools, temperature, format)
    _answered.set(OLLAMA_MODEL)
    return msg


def chat_stream(messages: list[dict], temperature: float = 0.0):
    """Falls back only when the cloud stream dies BEFORE the first delta —
    after that, restarting would show the viewer the same sentence twice. A
    mid-stream death surfaces as LLMError and the agent's text fallback
    handles it, which is rare and honest."""
    if _cloud_ready():
        from modules.cycle_time.chat import openai_compat
        yielded = False
        try:
            for piece in openai_compat.chat_stream(messages, temperature):
                yielded = True
                _answered.set(CHAT_MODEL)
                yield piece
            if yielded:
                return
        except LLMError as e:
            if yielded:
                raise
            log.warning("chat cloud stream failed (%s) — falling back to local", e)
    for piece in _local.chat_stream(messages, temperature):
        _answered.set(OLLAMA_MODEL)
        yield piece


__all__ = ["available", "chat", "chat_stream", "answered_by", "reset_answered", "rich",
           "MODEL", "LLMError"]


if __name__ == "__main__":
    print(f"provider={CHAT_PROVIDER}  primary={MODEL}")
    ok, detail = available()
    print("available:", ok, "-", detail)
    assert callable(chat) and callable(chat_stream)
    assert answered_by() is None
    print("llm self-check OK")
