"""
resolve.py  (cycle_time.chat)
─────────────────────────────
User words -> canonical keys. Deterministic, no LLM, no guessing.

THIS FILE IS WHY THE CHATBOT IS TRUSTWORTHY RATHER THAN A TOY
  The model routes and extracts arguments; it never decides what a name MEANS.
  "arista" is not a workcell — ARISTANETWORKS and ARISTA_NETWORKS_GLACIER both
  are, they are different customers, and they build different models. Letting an
  8B model pick between them silently attributes one workcell's numbers to
  another, and the answer still reads perfectly.

  So every name the model produces comes back through here first. If it does not
  resolve to exactly one thing, the tool returns the CANDIDATES instead of an
  answer, and the model asks the user which one. An ambiguous question gets a
  question back, never a confident wrong number.

WHAT THE MODEL GETS WRONG, MEASURED
  llama3.1:8b routed 6 of 7 realistic questions correctly. The miss was
  "how many % complete for arista" -> model_status(assembly="arista"), i.e. it
  put a customer name in the part-number slot. `assembly()` rejects that, because
  "ARISTA" is in no catalogue, and the tool answers "that is not a model, did you
  mean the workcell ARISTANETWORKS?" — which is recoverable. A resolver that
  shrugged and returned nothing would have produced "no data found" for a
  question with a perfectly good answer.
"""

from __future__ import annotations

import difflib
import re
from functools import lru_cache

import pandas as pd

from modules.cycle_time.config import CT_CUSTOMERS, CT_MART
from modules.cycle_time.model_universe import STATUSES, canon, norm


class Ambiguous(Exception):
    """More than one real thing matches. Carries the candidates so the caller can
    ask rather than pick."""

    def __init__(self, kind: str, given: str, options: list[str]):
        self.kind, self.given, self.options = kind, given, options
        super().__init__(f"{given!r} matches {len(options)} {kind}s: {', '.join(options[:8])}")


class NotFound(Exception):
    """Nothing matches. Carries the nearest few, because 'not found' with no
    suggestion is where a user gives up."""

    def __init__(self, kind: str, given: str, near: list[str]):
        self.kind, self.given, self.near = kind, given, near
        super().__init__(f"no {kind} matching {given!r}"
                         + (f". Closest: {', '.join(near)}" if near else ""))


@lru_cache(maxsize=1)
def _workcells() -> list[str]:
    """Every workcell we will answer about, in config spelling."""
    return sorted({c["customer"] for c in CT_CUSTOMERS})


def workcell(name: str) -> str:
    """User's word -> the ONE workcell name the marts use.

    Exact (normalised) first, then canon() so an alias like Cohu lands on LTX,
    then prefix, then fuzzy. Prefix is where ambiguity actually shows up:
    "arista" is a prefix of two real workcells and must NOT silently pick one.
    """
    raw = str(name or "").strip()
    if not raw:
        raise NotFound("workcell", raw, _workcells()[:5])

    want, wc = norm(raw), _workcells()
    by_norm = {norm(w): w for w in wc}
    if want in by_norm:
        return by_norm[want]

    # canon() folds documented aliases (COHU -> LTX). Match on the canonical key.
    ck = canon(raw)
    by_canon = {canon(w): w for w in wc}
    if ck in by_canon:
        return by_canon[ck]

    hits = [w for w in wc if norm(w).startswith(want)] or \
           [w for w in wc if want in norm(w)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise Ambiguous("workcell", raw, hits)

    near = difflib.get_close_matches(want, list(by_norm), n=3, cutoff=0.6)
    raise NotFound("workcell", raw, [by_norm[n] for n in near])


def status(name: str) -> str:
    """User's word -> one of the six verdicts. Accepts the labels people say
    ("missing ct", "not in iedb") as well as the keys."""
    raw = str(name or "").strip()
    k = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    spoken = {
        "missing_ct": "incomplete", "missing_cycle_time": "incomplete",
        "no_ct": "no_cycle_time", "untimed": "no_cycle_time",
        "not_in_ie_db": "not_in_iedb", "missing_in_iedb": "not_in_iedb",
        "not_built_yet": "not_built", "no_production": "not_built",
        "cannot_be_checked": "cannot_check", "not_on_mes": "cannot_check",
        "done": "complete", "finished": "complete",
    }
    k = spoken.get(k, k)
    if k in STATUSES or k == "not_checked":
        return k
    near = difflib.get_close_matches(k, STATUSES + ["not_checked"], n=3, cutoff=0.5)
    raise NotFound("status", raw, near)


@lru_cache(maxsize=1)
def _catalog() -> pd.DataFrame:
    """(workcell, normalised assembly, display assembly) for every model we know.
    Read once — this is the universe mart, 57k rows."""
    from modules.cycle_time.model_universe import build
    u = build()
    return u[["wc", "a", "assembly", "workcell"]].drop_duplicates(["wc", "a"])


def assembly(name: str, wc: str | None = None) -> tuple[str, str]:
    """Part number -> (workcell, display assembly).

    `wc` narrows the search when the user named one. Without it a model that two
    workcells both build is AMBIGUOUS and says so — LAMMEC and LAM RESEARCH
    building 620-12345 are two different models with two different routes, and
    picking either is a coin flip dressed up as an answer.
    """
    raw = str(name or "").strip()
    key = norm(raw)
    if not key:
        raise NotFound("model", raw, [])

    cat = _catalog()
    if wc:
        cat = cat[cat["wc"] == canon(wc)]

    hit = cat[cat["a"] == key]
    if len(hit) == 1:
        r = hit.iloc[0]
        return str(r["workcell"]), str(r["assembly"])
    if len(hit) > 1:
        raise Ambiguous("model", raw, sorted({f"{r.workcell} / {r.assembly}" for r in hit.itertuples()}))

    # Partial: people quote a base number and drop the suffix.
    part = cat[cat["a"].str.startswith(key)] if len(key) >= 4 else cat.iloc[0:0]
    opts = sorted({f"{r.workcell} / {r.assembly}" for r in part.itertuples()})
    if len(opts) == 1:
        r = part.iloc[0]
        return str(r["workcell"]), str(r["assembly"])
    if 1 < len(opts) <= 25:
        raise Ambiguous("model", raw, opts)

    near = difflib.get_close_matches(key, cat["a"].tolist()[:40000], n=3, cutoff=0.75)
    lut = cat.set_index("a")
    raise NotFound("model", raw, [f"{lut.loc[n, 'workcell']} / {lut.loc[n, 'assembly']}" for n in near])


def search(query: str, limit: int = 20) -> list[dict]:
    """Free-text model lookup. Substring, not fuzzy: an engineer typing part of a
    part number wants the ones that contain it, not something that looks like it."""
    key = norm(query)
    if len(key) < 2:
        return []
    cat = _catalog()
    hit = cat[cat["a"].str.contains(re.escape(key), regex=True)]
    return [{"workcell": str(r.workcell), "assembly": str(r.assembly)}
            for r in hit.head(limit).itertuples()]


if __name__ == "__main__":
    # Offline-ish self-check. Uses the real config for workcells (cheap) and only
    # touches the universe mart for the assembly cases.
    assert workcell("KEYSIGHT") == "KEYSIGHT"
    assert workcell("keysight") == "KEYSIGHT"
    assert workcell("  Key Sight ") == "KEYSIGHT", workcell("  Key Sight ")
    try:
        workcell("arista"); raise SystemExit("FAIL: 'arista' must be ambiguous")
    except Ambiguous as e:
        assert len(e.options) >= 2, e.options
    try:
        workcell("zzzznope"); raise SystemExit("FAIL: unknown workcell must raise")
    except NotFound:
        pass
    assert status("missing ct") == "incomplete"
    assert status("no_cycle_time") == "no_cycle_time"
    assert status("Not In IEDB") == "not_in_iedb"
    try:
        status("banana"); raise SystemExit("FAIL: unknown status must raise")
    except NotFound:
        pass
    print("resolve self-check OK")
