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
import re
import time
import urllib.error
import urllib.request

from modules.cycle_time.config import (
    CHAT_API_BASE, CHAT_API_KEY, CHAT_MODEL, OLLAMA_TIMEOUT,
)
from modules.cycle_time.chat.ollama import OllamaError

log = logging.getLogger(__name__)

# Jabil's TLS interception re-signs every outbound cert with a corporate CA
# that Python's bundled list rejects ("Basic Constraints of CA cert not marked
# critical"). truststore validates against the Windows cert store instead —
# the same fix mes_webapi.py ships for the same network.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception as _e:                          # noqa: BLE001
    log.warning("truststore unavailable (%s) — cloud TLS may fail behind the proxy", _e)


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
                 "Authorization": f"Bearer {CHAT_API_KEY}",
                 # Cloudflare fronts Groq and 403s (error 1010) the default
                 # "Python-urllib/3.x" agent. Any honest product string passes.
                 "User-Agent": "IE-Pulse-Chat/1.0"},
    )


def _strict(schema):
    """Strict json_schema mode requires additionalProperties:false on EVERY
    object — providers 400 without it. Deep-set on a copy; the callers' schemas
    stay as written."""
    if isinstance(schema, dict):
        out = {k: _strict(v) for k, v in schema.items()}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [_strict(v) for v in schema]
    return schema


def _body(messages, temperature, format, stream, tools=None):
    body = {"model": CHAT_MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": 900, "stream": stream}
    if tools:
        body["tools"] = tools
        # gpt-oss spends REASONING tokens inside max_tokens before the visible
        # answer — 1500 left it planning eight-word finals. But Groq counts
        # max_tokens as a RESERVATION against the 8k/min budget, so 4000 made
        # every call request ~7.7k and 429 on arrival. 2000 = think + write.
        body["max_tokens"] = 2000
    if format == "json":
        # json_object mode: the API insists the word "json" appears in the
        # messages, and the mode itself carries no schema — restate it.
        body["response_format"] = {"type": "json_object"}
        body["messages"] = messages + [{"role": "system",
            "content": "Respond ONLY with a JSON object matching the schema "
                       "described above."}]
    elif format:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "form", "strict": True,
                            "schema": _strict(format)}}
    return body


def chat(messages: list[dict], tools=None, temperature: float = 0.0,
         format: dict | str | None = None) -> dict:
    """One round trip. Returns the `message` object, same shape ollama.chat
    gives back ({"content": ...}). `tools` accepted for signature parity and
    used natively by the rich agent loop (OpenAI tool-calling format)."""
    waits_429 = 0
    for attempt, fmt in enumerate((format, "json" if isinstance(format, dict) else None)):
        if attempt and fmt is None:
            break
        while True:                              # at most one 429 wait per fmt
            try:
                with urllib.request.urlopen(_request(_body(messages, temperature, fmt, False, tools)),
                                            timeout=OLLAMA_TIMEOUT) as r:
                    data = json.load(r)
                msg = (data.get("choices") or [{}])[0].get("message")
                if not isinstance(msg, dict):
                    raise OllamaError(f"unexpected response: {str(data)[:200]}")
                return msg
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # Free-tier TPM refills by the second and the 429 body says
                # when ("try again in 1.4s"). ONE polite wait keeps a multi-
                # round agent turn alive instead of failing it over a bump.
                if e.code == 429 and waits_429 < 2:
                    waits_429 += 1
                    m = re.search(r"in (\d+(?:\.\d+)?)s", detail)
                    time.sleep(min(float(m.group(1)) if m else 2.0, 15.0) + 0.2)
                    continue
                # A provider that rejects json_schema gets ONE retry as
                # json_object; anything else is a real failure.
                if e.code in (400, 422) and isinstance(format, dict) and attempt == 0:
                    log.info("chat cloud: json_schema rejected, retrying as json_object")
                    break                        # next fmt
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
