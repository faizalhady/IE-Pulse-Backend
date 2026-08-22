"""
modules/universe/eval/chain.py
──────────────────────────────
The free-model chain. Always start from the top, skip anything on cooldown, never
move a pointer — slot 1 comes back by itself the moment its window resets. A slot
without a key is simply not there. Ollama is the last resort, outage-only.

    python -m modules.universe.eval.chain          # slot table: key present? on cooldown? one-token ping
    python -m modules.universe.eval.run --provider chain

Cooldowns (vault: Todo/Free LLM Fallback Chain): a 429 blocks the slot for what the
provider says (Retry-After header, "try again in 12.5s", or "per day" → until midnight
UTC); anything else that fails — 5xx, timeout, bad key, a 413 that slot cannot take —
blocks it for a minute and the next slot gets the same messages.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENROUTER = "https://openrouter.ai/api/v1"
GROQ = "https://api.groq.com/openai/v1"
CEREBRAS = "https://api.cerebras.ai/v1"
MISTRAL = "https://api.mistral.ai/v1"
MAX_WAIT_S = 90          # wait for a cooling slot this long at most; beyond it, the chain is down


@dataclass
class Slot:
    name: str
    base: str
    key_env: str | None          # None = no key needed (Ollama)
    model: str
    blocked_until: float = 0.0
    calls: int = 0
    tokens: int = 0
    last_error: str = ""

    @property
    def key(self) -> str:
        return os.getenv(self.key_env, "") if self.key_env else ""

    @property
    def available(self) -> bool:
        return (self.key_env is None or bool(self.key)) and self.blocked_until <= time.time()


# The order from the plan note: burn the small good quotas first, big quotas are the floor,
# local is outage-only. Slots whose key is missing are skipped, not errors.
# Model ids are what each provider's GET /models returned on 2026-08-23 — the README's list
# (gemini-3-flash, gpt-oss-120b:free on OpenRouter, gpt-oss-120b on Cerebras, llama-3.x on Groq)
# was already gone. Re-check with `python -m modules.universe.eval.chain` when a slot 404s.
SLOTS: list[Slot] = [
    Slot("gemini-3.7-flash",        GEMINI,     "GEMINI_API_KEY",     "gemini-3.7-flash"),
    Slot("or-nemotron-3-ultra",     OPENROUTER, "OPENROUTER_API_KEY", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    Slot("or-glm-5.2",              OPENROUTER, "OPENROUTER_API_KEY", "z-ai/glm-5.2:free"),
    Slot("gemini-3.5-flash-lite",   GEMINI,     "GEMINI_API_KEY",     "gemini-3.5-flash-lite"),
    Slot("groq-gpt-oss-120b",       GROQ,       "CHAT_API_KEY",       "openai/gpt-oss-120b"),
    Slot("mistral-medium",          MISTRAL,    "MISTRAL_API_KEY",    "mistral-medium-latest"),
    Slot("cerebras-gemma-4-31b",    CEREBRAS,   "CEREBRAS_API_KEY",   "gemma-4-31b"),
    Slot("gemini-gemma-4-31b",      GEMINI,     "GEMINI_API_KEY",     "gemma-4-31b-it"),
    Slot("or-nemotron-3-super-120b", OPENROUTER, "OPENROUTER_API_KEY", "nvidia/nemotron-3-super-120b-a12b:free"),
    Slot("or-gemma-4-31b",          OPENROUTER, "OPENROUTER_API_KEY", "google/gemma-4-31b-it:free"),
    Slot("mistral-small",           MISTRAL,    "MISTRAL_API_KEY",    "mistral-small-latest"),
    # the floor: every miss in the exam runs of 2026-08-23 came from these two (invented a table
    # of test counts, narrated SQL instead of running it, dropped the caveat). Outage-only.
    Slot("groq-qwen3.6-27b",        GROQ,       "CHAT_API_KEY",       "qwen/qwen3.6-27b"),
    Slot("groq-gpt-oss-20b",        GROQ,       "CHAT_API_KEY",       "openai/gpt-oss-20b"),
    Slot("ollama",                os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434") + "/v1", None,
         os.getenv("OLLAMA_MODEL", "llama3.1:8b")),
]

trace: list[str] = []       # slot name per successful call; run.py copies it into each question's record
events: list[str] = []      # why a slot was skipped (429 -> 60s, HTTP 402 …); the run explains itself


class RateLimited(Exception):
    def __init__(self, seconds: float, text: str):
        super().__init__(text)
        self.seconds = seconds


def _retry_seconds(r: httpx.Response) -> float:
    """How long the provider wants us away. Daily caps → until midnight UTC."""
    if re.search(r"per day|daily|TPD|RPD", r.text, re.I):
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight - now).total_seconds()
    if r.headers.get("retry-after", "").replace(".", "").isdigit():
        return float(r.headers["retry-after"])
    m = re.search(r"try again in ([\d.]+)\s*(ms|s|m)", r.text, re.I)
    if m:
        n, unit = float(m.group(1)), m.group(2).lower()
        return n / 1000 if unit == "ms" else n * 60 if unit == "m" else n
    return 60.0


def _call(slot: Slot, messages, tools_spec, tool_choice, max_tokens: int, temperature: float) -> dict:
    body = {"model": slot.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools_spec:
        body.update({"tools": tools_spec, "tool_choice": tool_choice})
    if "gpt-oss" in slot.model:
        body["reasoning_effort"] = "low"           # the budget is tokens per minute, not brains
    headers = {"Authorization": f"Bearer {slot.key}"} if slot.key else {}
    r = httpx.post(f"{slot.base.rstrip('/')}/chat/completions", json=body, timeout=180, headers=headers)
    if r.status_code == 429:
        raise RateLimited(_retry_seconds(r), r.text[:200])
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    usage = data.get("usage", {}) or {}
    slot.calls += 1
    slot.tokens += int(usage.get("total_tokens") or 0)
    return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls"), "usage": usage, "slot": slot.name}


def chat(messages, tools_spec=None, tool_choice="auto", max_tokens: int = 1500, temperature: float = 0.0) -> dict:
    """Walk the slots top-down; the first available one answers. A failure cools the
    slot and the NEXT slot gets the same messages — mid-conversation hand-off is fine,
    every slot speaks the same protocol."""
    errors = []
    deadline = time.time() + MAX_WAIT_S
    while True:
        for slot in SLOTS:
            if not slot.available:
                continue
            try:
                out = _call(slot, messages, tools_spec, tool_choice, max_tokens, temperature)
                trace.append(slot.name)
                return out
            except RateLimited as e:
                slot.blocked_until = time.time() + e.seconds
                slot.last_error = f"429 -> {e.seconds:.0f}s"
                errors.append(f"{slot.name}: {slot.last_error}")
                events.append(f"{slot.name}: {slot.last_error}")
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
                # a 401/402/403/404 is the key, the plan or the model id — it will not heal in a
                # minute; a 5xx or a timeout might. (413 = this request, not the slot: a minute.)
                hard = str(e).startswith("HTTP 4") and not str(e).startswith("HTTP 413")
                slot.blocked_until = time.time() + (24 * 3600 if hard else 60)
                slot.last_error = str(e)[:120]
                errors.append(f"{slot.name}: {slot.last_error}")
                events.append(f"{slot.name}: {slot.last_error[:60]}")
        # every slot is cooling. With one provider on one key that is the normal minute
        # limit, not an outage: wait for the soonest slot when the wait is short.
        keyed = [s for s in SLOTS if s.key_env is None or s.key]
        soonest = min((s.blocked_until for s in keyed), default=0)
        if not keyed or soonest > deadline:
            break
        time.sleep(max(soonest - time.time(), 0) + 0.5)
    raise RuntimeError("every slot is down or cooling: " + " | ".join(errors[-6:]))


def take_trace() -> list[str]:
    out, trace[:] = list(trace), []
    return out


def take_events() -> list[str]:
    out, events[:] = list(events), []
    return out


def status(ping: bool = False) -> list[dict]:
    rows = []
    for s in SLOTS:
        row = {"slot": s.name, "model": s.model, "key": "—" if s.key_env is None else ("yes" if s.key else "MISSING"),
               "cooldown_s": max(0, math.ceil(s.blocked_until - time.time())), "calls": s.calls, "tokens": s.tokens, "ping": ""}
        if ping and (s.key or s.key_env is None):
            try:
                _call(s, [{"role": "user", "content": "Reply with the single word: ok"}], None, "none", 5, 0.0)
                row["ping"] = "ok"
            except RateLimited as e:
                row["ping"] = f"429 ({e.seconds:.0f}s)"
            except Exception as e:                   # noqa: BLE001
                row["ping"] = str(e)[:80]
        rows.append(row)
    return rows


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:                                 # noqa: BLE001
        pass
    for r in status(ping=True):
        print(f"{r['slot']:<24} {r['model']:<44} key={r['key']:<8} {r['ping']}")
