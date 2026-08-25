"""
plants.py - the plant/site names, once, for every module.

WHY THIS EXISTS
    `ebuild/customer_plant.parquet` mixes two naming systems in one column:
    site codes (JBK, JPE) next to plant numbers (Plant 1, Plant 3). Anything
    grouping on that column treats four labels as four peers when they are
    really two names for some of the same places. Fix the vocabulary once here
    rather than in each module that touches it.

CONFIRMED (Faiz, 2026-08-25)
    JPE  = Plant 2
    JBK  = Batu Kawan

OPEN
    Does Batu Kawan carry a plant number? It is kept as its own canonical name
    until someone says otherwise - inventing "Plant 4" would be a guess that
    later reads as a fact. ❓
"""
from __future__ import annotations

import re

#: canonical name -> every spelling seen in the data or said out loud.
PLANTS: dict[str, tuple[str, ...]] = {
    "Plant 1":    ("PLANT1", "P1", "JPN1"),
    "Plant 2":    ("PLANT2", "P2", "JPE"),
    "Plant 3":    ("PLANT3", "P3"),
    "Batu Kawan": ("JBK", "BK", "BATUKAWAN", "BATU KAWAN"),
}

_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())

#: alias key -> canonical, including each canonical as its own alias.
_LOOKUP = {_norm(c): c for c in PLANTS} | {
    _norm(a): c for c, aliases in PLANTS.items() for a in aliases
}


def canon(name: str | None) -> str | None:
    """Canonical plant name, or None when it is not a plant we know.

    None is deliberate: an unrecognised label must not be silently kept as if
    it were a plant, and must not be dropped either - the caller decides.
    """
    return _LOOKUP.get(_norm(name)) if name is not None else None


def demo() -> None:
    assert canon("JBK") == "Batu Kawan"
    assert canon("Batu Kawan") == "Batu Kawan"
    assert canon("JPE") == canon("Plant 2") == canon("p2") == "Plant 2"
    assert canon("Plant 1") == canon("P1") == "Plant 1"
    assert canon("Plant 3") == "Plant 3"
    assert canon("nowhere") is None and canon(None) is None
    print("core/plants.py ok -", ", ".join(PLANTS))


if __name__ == "__main__":
    demo()
