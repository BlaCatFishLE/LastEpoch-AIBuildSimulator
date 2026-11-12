"""Ad-hoc search inside Tunklab JS chunks (development helper)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "data" / "raw" / "_probe" / "tunklab"


def main() -> int:
    needles = sys.argv[1:]
    if not needles:
        print("usage: tunklab_search.py NEEDLE [NEEDLE...]")
        return 2
    for path in sorted(PROBE.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in needles:
            start = 0
            hits = 0
            while hits < 3:
                idx = text.find(needle, start)
                if idx < 0:
                    break
                print(f"\n[{path.name}] {needle!r} @ {idx}")
                print("   ", text[max(0, idx - 120): idx + 220].replace("\n", " "))
                start = idx + 1
                hits += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
