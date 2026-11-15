"""Tilläggets tester körs utan Docker: ``python -m pytest drivelog/tests``."""

import sys
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADDON / "rootfs" / "opt" / "drivelog-addon"))
