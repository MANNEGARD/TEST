#!/usr/bin/env python3
"""Skriver de anonymiserade exempelexporterna (Volvo-körjournal) till en katalog.

Användning:
    backend/.venv/bin/python scripts/make_sample_exports.py <målkatalog>

Filerna kommer från backend/tests/fixtures/volvo_exports.py (sample_files()) och
används av frontendens röktest (frontend/e2e/smoke.mjs) och för manuell provning.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from tests.fixtures.volvo_exports import sample_files  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Användning: make_sample_exports.py <målkatalog>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    target.mkdir(parents=True, exist_ok=True)
    for name, data in sample_files():
        path = target / name
        path.write_bytes(data)
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
