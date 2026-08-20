"""
eval.py  (cycle_time.chat)
──────────────────────────
The routing eval: does each question land in the right lane, on the right tool?

Run:  python -m modules.cycle_time.chat.eval          # needs Ollama running

This is what "6/7" grows into. A prompt tweak, a model swap, a new tool — run
this before trusting it. Checks LANE always and INTENT when one is expected;
it does NOT judge the prose, because routing is the part that decides whether
the number is right, and the number is computed by code anyway.

Cases come from real turns (logs/chat_turns.jsonl) — when a live question
routes wrong, it gets added here with the intent it SHOULD have had.
"""

from __future__ import annotations

import sys
import time

from modules.cycle_time.chat import ollama
from modules.cycle_time.chat.agent import ask

# (question, expected lane, expected intent or None for "don't care")
CASES: list[tuple[str, str, str | None]] = [
    # instant — code answers, no model
    ("hello",                                   "instant",   None),
    ("tq",                                      "instant",   None),
    ("bye",                                     "instant",   None),
    ("what can you do",                         "instant",   None),
    ("how are you",                             "instant",   None),
    # general — not our data
    ("what is the capital of france",           "general",   None),
    ("translate good morning to malay",         "general",   None),
    ("write a haiku about rain",                "general",   None),
    # cycletime — one tool each
    ("how many % complete for keysight",        "cycletime", "workcell_completion"),
    ("how complete is arista",                  "cycletime", "workcell_completion"),
    ("overall completion for the plant",        "cycletime", "plant_completion"),
    ("how are we doing overall",                "cycletime", "plant_completion"),
    ("which keysight models have no cycle time", "cycletime", "models_by_status"),
    ("list lam research models not in iedb",    "cycletime", "models_by_status"),
    ("status of PCA-01247-01",                  "cycletime", "model_status"),
    ("show me the bom for PCA-01156-15",        "cycletime", "model_bom"),
    ("cycle time steps for PCA-01247-01",       "cycletime", "model_cycle_time"),
    ("list all workcells",                      "cycletime", "list_workcells"),
    ("find models containing 01247",            "cycletime", "search_models"),
    ("completion trend for keysight",           "cycletime", "completion_trend"),
    ("how did completion move the last weeks",  "cycletime", "completion_trend"),
    # cycletime concept — no tool answers it, the glossary does
    ("what does cannot_check mean",             "cycletime", "none"),
    ("what is a workcell",                      "cycletime", "none"),
    ("what does not_built mean",                "cycletime", "none"),
    ("define no_cycle_time",                    "cycletime", "none"),
]


def main() -> int:
    ok_avail, detail = ollama.available()
    if not ok_avail:
        print(f"SKIP: Ollama unavailable — {detail}")
        return 2

    passed, failed, t0 = 0, [], time.time()
    for q, want_lane, want_intent in CASES:
        r = ask(q)
        lane_ok = r["lane"] == want_lane
        intent_ok = want_intent is None or r["intent"] == want_intent
        ok = lane_ok and intent_ok
        passed += ok
        if not ok:
            failed.append((q, want_lane, want_intent, r["lane"], r["intent"]))
        mark = "ok " if ok else "FAIL"
        print(f"{mark} {r['elapsed_s']:5.1f}s  {r['lane']:9} {r['intent']:20} {q}")

    print(f"\n{passed}/{len(CASES)} passed in {time.time() - t0:.0f}s")
    for q, wl, wi, gl, gi in failed:
        print(f"  FAIL {q!r}: wanted {wl}/{wi or '*'}, got {gl}/{gi}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
