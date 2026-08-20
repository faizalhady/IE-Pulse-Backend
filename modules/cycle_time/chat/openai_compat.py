"""
openai_compat.py  (cycle_time.chat)
───────────────────────────────────
The cloud seam: one client for every OpenAI-compatible provider.

Groq, Gemini (its /openai endpoint), OpenRouter, Cerebras and NVIDIA NIM all
speak this protocol, so "test another alternative" is three env values, never
a code change. Same surface as ollama.py — chat(), chat_stream(), available()
— and it raises ollama.OllamaError so every existing except-clause holds.

STRUCTURED OUTPUT
  `format` (a JSON schema) maps to response_format json_schema. Providers that
  reject it get one retry as json_object — the router validates every field
  and enum against the schema anyway, so a looser mode degrades to the same
  checked result, not to trust.

NO SEMAPHORE
  The GPU lock in ollama.py exists because one 8 GB card runs one generation.
  A cloud endpoint handles concurrency itself; serialising 5 users through a
  local lock would just rebuild the queue we paid to remove.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from modules.cycle_time.config import (
    CHAT_API_BASE, CHAT_API_KEY, CHAT_MODEL, OLLAMA_TIMEOUT,
)
from modules.cycle_time.chat.ollama import OllamaError

log = logging.getLogger(__name__)


def available() -> tuple[bool, str]:
    if not (CHAT_API_BASE and CHAT_API_KEY and CHAT_MODEL):
        missing = [n for n, v in (("CHAT_API_BASE", CHAT_API_BASE),
                                  ("CHAT_API_KEY", CHAT_API_KEY),
                                  ("CHAT_MODEL", CHAT_MODEL)) if not v]
        return False, f"cloud chat not configured: set {', '.join(missing)}"
    return True, CHAT_MODEL


def _request(body: dict) -> urllib.request.Request:
    return urllib.request.Request(
        f"{CHAT_API_BASE}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {CHAT_API_KEY}"},
    )


def _body(messages, temperature, format, stream):
    body = {"model": CHAT_MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": 500, "stream": stream}
    if format:
        body["response_format"] = (
            {"type": "json_object"} if format == "json" else
            {"type": "json_schema",
             "json_schema": {"name": "form", "strict": True, "schema": format}})
    return body


def chat(messages: list[dict], tools=None, temperature: float = 0.0,
         format: dict | str | None = None) -> dict:
    """One round trip. Returns the `message` object, same shape ollama.chat
    gives back ({"content": ...}). `tools` accepted for signature parity and
    unused — this chat routes by form, not by native tool-calling."""
    for attempt, fmt in enumerate((format, "json" if isinstance(format, dict) else None)):
        if attempt and fmt is None:
            break
        try:
            with urllib.request.urlopen(_request(_body(messages, temperature, fmt, False)),
                                        timeout=OLLAMA_TIMEOUT) as r:
                data = json.load(r)
            msg = (data.get("choices") or [{}])[0].get("message")
            if not isinstance(msg, dict):
                raise OllamaError(f"unexpected response: {str(data)[:200]}")
            return msg
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            # A provider that rejects json_schema gets ONE retry as
            # json_object; anything else is a real failure.
            if e.code in (400, 422) and isinstance(format, dict) and attempt == 0:
                log.info("chat cloud: json_schema rejected, retrying as json_object")
                continue
            raise OllamaError(f"cloud chat HTTP {e.code}: {detail}") from e
        except OllamaError:
            raise
        except Exception as e:
            raise OllamaError(f"cloud chat failed: {e}") from e
    raise OllamaError("cloud chat: structured output rejected twice")


def chat_stream(messages: list[dict], temperature: float = 0.0):
    """Yield content deltas. SSE frames: `data: {...}` lines, [DONE] ends."""
    try:
        with urllib.request.urlopen(_request(_body(messages, temperature, None, True)),
                                    timeout=OLLAMA_TIMEOUT) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                piece = ((chunk.get("choices") or [{}])[0]
                         .get("delta") or {}).get("content") or ""
                if piece:
                    yield piece
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"cloud stream HTTP {e.code}: {detail}") from e
    except OllamaError:
        raise
    except Exception as e:
        raise OllamaError(f"cloud stream failed: {e}") from e


if __name__ == "__main__":
    ok, detail = available()
    print(f"cloud chat: {'CONFIGURED' if ok else 'NOT CONFIGURED'} — {detail}")
    b = _body([{"role": "user", "content": "x"}], 0.0, {"type": "object"}, False)
    assert b["response_format"]["type"] == "json_schema"
    assert _body([], 0.0, "json", False)["response_format"] == {"type": "json_object"}
    assert "response_format" not in _body([], 0.0, None, True)
    print("openai_compat self-check OK")
