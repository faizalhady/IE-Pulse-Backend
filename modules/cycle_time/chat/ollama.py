"""
ollama.py  (cycle_time.chat)
────────────────────────────
Thin client for a local Ollama server. Same shape as mes_webapi.py: one POST
helper, explicit errors, no cleverness.

WHY OLLAMA IS THE SEAM, NOT A MICROSERVICE
  The tools ARE backend functions reading process-local caches — `_completion_
  demand` alone is a 32 MB cached payload and `model_universe` is 17 MB. Splitting
  the chat out into its own service would make it re-read every mart or pull those
  payloads over HTTP per question, slower than the model it is calling. So the
  chat module lives in this backend, and the only thing that genuinely needs its
  own process — the GPU — already has one.

  That makes GPU LOCATION a config value. `OLLAMA_BASE_URL` points at localhost
  while the card is in a workstation, and at a server later, with nothing else
  changing.

ONE CARD, ONE REQUEST
  An 8 GB card serves roughly one generation at a time. Concurrent requests do not
  fail, they queue inside Ollama and every one of them gets slower with no
  feedback. A semaphore here makes the queue OURS, so a caller can be told it is
  waiting instead of assuming the app hung.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from modules.cycle_time.config import (
    OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX, OLLAMA_TIMEOUT,
)

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Any failure talking to Ollama — not running, model missing, bad response."""


#: One generation at a time. See the module docstring.
_gpu = threading.Semaphore(1)


def available() -> tuple[bool, str]:
    """(reachable, detail). Used by /chat/health so the UI can say "Ollama is not
    running" instead of showing a spinner forever."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
        if OLLAMA_MODEL not in names:
            return False, (f"model {OLLAMA_MODEL!r} not pulled. Run: ollama pull {OLLAMA_MODEL}. "
                           f"Available: {', '.join(names) or 'none'}")
        return True, OLLAMA_MODEL
    except Exception as e:
        return False, f"cannot reach Ollama at {OLLAMA_BASE_URL}: {e}"


def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.0, format: dict | str | None = None) -> dict:
    """One /api/chat round trip. Returns the `message` object.

    temperature 0 by default: this is routing and argument extraction, where a
    sampled answer is just a less reliable one.

    `format` is a JSON schema (Ollama structured outputs): the decoder masks
    every token that would break the schema, so the reply IS the schema — the
    model cannot pick a nonexistent enum value or wander into prose. This is a
    grammar constraint, not a request, which is why the router trusts it.

    keep_alive -1 pins the model in VRAM. Loading 5 GB is seconds; a chatbot
    that pays that on the first question after every idle period feels broken.
    """
    body = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        # num_predict caps a runaway generation; every legitimate reply here
        # is a routing form or one short sentence, far under 300 tokens.
        "options": {"temperature": temperature, "num_ctx": OLLAMA_NUM_CTX,
                    "num_predict": 300},
    }
    if tools:
        body["tools"] = tools
    if format:
        body["format"] = format

    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with _gpu:
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise OllamaError(f"Ollama HTTP {e.code}: {detail}") from e
        except Exception as e:
            raise OllamaError(f"Ollama request failed: {e}") from e

    msg = data.get("message")
    if not isinstance(msg, dict):
        raise OllamaError(f"unexpected Ollama response: {str(data)[:200]}")
    return msg


def tool_calls(msg: dict) -> list[tuple[str, dict]]:
    """[(name, args)] from a message. Ollama returns arguments as an object, but
    some builds hand back a JSON STRING — both are normalised here so the agent
    never has to care."""
    out = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        name = fn.get("name")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name:
            out.append((name, args if isinstance(args, dict) else {}))
    return out


if __name__ == "__main__":
    ok, detail = available()
    print(f"ollama: {'OK' if ok else 'UNAVAILABLE'} — {detail}")
    # Argument normalisation is the only local logic worth checking offline.
    assert tool_calls({"tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}]}) == [("f", {"a": 1})]
    assert tool_calls({"tool_calls": [{"function": {"name": "f", "arguments": '{"a": 1}'}}]}) == [("f", {"a": 1})]
    assert tool_calls({"tool_calls": [{"function": {"name": "f", "arguments": "not json"}}]}) == [("f", {})]
    assert tool_calls({}) == []
    print("ollama self-check OK")
