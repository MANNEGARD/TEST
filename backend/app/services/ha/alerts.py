"""Larm om kopplingarna: Volvo, kalendern och en tyst bil (byggkontraktet C.5).

STUBBE (G0): ``run`` gör inget förrän S2b fyller modulen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session


def run(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Prövar larmvillkoren (högst var 15:e minut). (Stubbe: inga larm.)"""
    return {}
