"""No non-ASCII inside a log or print call, anywhere.

Windows hands a redirected stdout the cp1252 codepage. A character cp1252
cannot encode makes logging raise UnicodeEncodeError, and that kills the
process. On 2026-08-06 a single U+2713 ended a 4,126-model completion rebuild
on 02 after one customer, with nothing in the log to say why.

stdout is forced to UTF-8 where we control the entry point, but scheduled
tasks, services and other people's shells are not ours to control. Plain ASCII
in anything that reaches a stream is the belt to that braces.

Comments and docstrings are exempt -- they never reach a stream.

Run: python tests/test_log_ascii.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROOTS = ("modules", "scripts", "api", "core")
CALL = re.compile(r"\b(?:log|logging|logger|_log)\.(?:info|warning|warn|error|debug|exception|critical)\s*\(|\bprint\s*\(")


def offenders() -> list[tuple[str, int, str, list[str]]]:
    out = []
    for r in ROOTS:
        for f in sorted((ROOT / r).rglob("*.py")):
            if "__pycache__" in str(f):
                continue
            depth = 0
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                m = CALL.search(line)
                if depth > 0 or m:
                    bad = sorted({c for c in line if ord(c) > 127})
                    if bad:
                        out.append((str(f.relative_to(ROOT)), n, line.strip()[:70], bad))
                    seg = line if depth > 0 else line[m.start():]
                    depth = max(0, depth + seg.count("(") - seg.count(")"))
    return out


def main() -> None:
    bad = offenders()
    if bad:
        for f, n, line, chars in bad:
            print(f"{f}:{n}  {[hex(ord(c)) for c in chars]}  {line}")
        raise SystemExit(f"FAIL - {len(bad)} log/print lines carry non-ASCII")
    print("ok - no non-ASCII in any log or print call")


if __name__ == "__main__":
    main()
