"""Användarinställningar med standardvärden och validering.

Inställningarna lagras som JSON per nyckel (tabellen ``settings``). Okända nycklar
avvisas. Standardvärdena följer specifikationen (15/60 minuter osv.) men allt kan
ändras i webbgränssnittet.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "timezone": "Europe/Stockholm",
        "driver_name": "",
        "employer_name": "",
    },
    "merge": {
        "auto_max_minutes": 15,  # Regel A: ≤ 15 min → automatisk sammanslagning
        "ask_max_minutes": 60,  # Regel B: > 15 och ≤ 60 min → fråga användaren
        "odometer_tolerance_km": 2.0,  # heltalsavrundning i mätarställning
        "enabled": True,
        # Verifierade importerade resor slås inte ihop automatiskt (Volvos app kan
        # redan ha slagit ihop dem); de ger i stället förslag i granskningscentret.
        "auto_merge_imported": False,
    },
    "ai": {
        "provider": "none",  # none | anthropic | openai
        "model": "",
        "auto_threshold": 0.85,  # ≥ → automatisk preliminär klassning
        "review_threshold": 0.60,  # ≥ → begär godkännande, annars obligatorisk granskning
        "strict_mode": False,  # allt AI/regel-beslutat måste godkännas
        "send_calendar_subjects": True,
        "send_calendar_body": False,
        "send_street_addresses": False,  # annars endast platsnamn/ort
        "use_llm_for_purpose": True,
    },
    "tax": {
        "car_benefit": True,  # bilförmån/förmånsbil
        "home_is_workplace": False,  # bostaden är tjänsteställe
        "commute_rule_enabled": True,  # tillämpa regeln bostad ↔ tjänsteställe
        "require_approval_when_uncertain": True,
    },
    "notifications": {
        "enabled": True,
        "kinds": {"classify": True, "purpose": True, "merge": True, "weekly": True, "daily": False},
        "quiet_start": "21:00",
        "quiet_end": "07:00",
        "max_per_day": 6,
        "batch_window_minutes": 10,
        "digest_threshold": 3,  # fler än så samtidigt → en samlingsnotis
        "reminder_after_hours": 20,
        "max_reminders": 1,
        "expire_after_hours": 72,
        "snooze_minutes": 120,
        "hide_locations_on_lock_screen": True,
        "weekly_day": 6,  # 0 = måndag … 6 = söndag
        "weekly_time": "18:00",
        "daily_time": "20:30",
        "delivery_mode": "push",  # push (backend → HA) | pull (HA hämtar)
    },
    "volvo": {
        "poll_interval_seconds": 300,
        "min_poll_interval_seconds": 120,
        "stale_after_minutes": 24 * 60,
        "position_threshold_m": 150,
        "min_odometer_delta_km": 1.0,
        "settle_minutes": 10,
        "detour_factor": 1.8,  # mätarsträcka / fågelväg innan "flera resor?" flaggas
        "detour_slack_km": 5.0,
        "max_unobserved_minutes": 45,
    },
    "calendar": {
        "enabled": True,
        "calendar_ids": [],
        "sync_days_back": 60,
        "sync_days_forward": 14,
        "arrival_window_before_minutes": 120,
        "arrival_window_after_minutes": 45,
        "min_match_score": 0.45,
        "store_body_preview": False,
    },
    "privacy": {
        "geocoding_enabled": False,  # skickar adresser till extern geokodare
        "geocoder_url": "https://nominatim.openstreetmap.org",
        "map_tiles_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "map_enabled": True,
        "observation_retention_days": 0,  # 0 = behåll
    },
    "import": {
        "default_coverage": "selection",
        "keep_original_files": True,
    },
    "export": {
        "include_private_addresses": False,  # privata resor redovisas utan detaljadresser
        "include_change_history": True,
    },
    "costs": {
        "fuel_price_per_liter": None,
        "electricity_price_per_kwh": None,
    },
    "abbreviations": {
        # Egna förkortningar som förekommer i kommentarer → fullständig ort/plats.
        "map": {"JKPG": "Jönköping", "HBG": "Helsingborg", "BLG": "Borlänge"},
    },
}

_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("merge", "auto_max_minutes"): (0, 240),
    ("merge", "ask_max_minutes"): (0, 24 * 60),
    ("merge", "odometer_tolerance_km"): (0, 20),
    ("ai", "auto_threshold"): (0.5, 1.0),
    ("ai", "review_threshold"): (0.0, 1.0),
    ("notifications", "max_per_day"): (0, 100),
    ("notifications", "batch_window_minutes"): (0, 240),
    ("notifications", "reminder_after_hours"): (1, 24 * 14),
    ("notifications", "max_reminders"): (0, 10),
    ("notifications", "expire_after_hours"): (1, 24 * 60),
    ("notifications", "weekly_day"): (0, 6),
    ("volvo", "poll_interval_seconds"): (60, 24 * 3600),
    ("volvo", "settle_minutes"): (1, 240),
    ("volvo", "position_threshold_m"): (20, 5000),
    ("calendar", "min_match_score"): (0.0, 1.0),
}


class SettingsError(ValueError):
    pass


def _validate_time(value: Any, key: str) -> None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise SettingsError(f"{key} måste anges som HH:MM.")
    hh, mm = value.split(":")
    if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        raise SettingsError(f"{key} måste anges som HH:MM.")


def _validate(section: str, values: dict[str, Any]) -> None:
    defaults = DEFAULTS[section]
    for key, value in values.items():
        if key not in defaults:
            raise SettingsError(f"Okänd inställning: {section}.{key}")
        default = defaults[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise SettingsError(f"{section}.{key} måste vara sant/falskt.")
        elif isinstance(default, (int, float)) and default is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SettingsError(f"{section}.{key} måste vara ett tal.")
            bounds = _BOUNDS.get((section, key))
            if bounds and not (bounds[0] <= value <= bounds[1]):
                raise SettingsError(f"{section}.{key} måste ligga mellan {bounds[0]:g} och {bounds[1]:g}.")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise SettingsError(f"{section}.{key} måste vara text.")
            if key.endswith(("_time", "_start", "_end")):
                _validate_time(value, f"{section}.{key}")
        elif isinstance(default, dict) and not isinstance(value, dict):
            raise SettingsError(f"{section}.{key} måste vara ett objekt.")
        elif isinstance(default, list) and not isinstance(value, list):
            raise SettingsError(f"{section}.{key} måste vara en lista.")

    if section == "general" and "timezone" in values:
        try:
            ZoneInfo(values["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError("Okänd tidszon.") from exc
    if section == "ai" and "provider" in values and values["provider"] not in {"none", "anthropic", "openai"}:
        raise SettingsError("ai.provider måste vara none, anthropic eller openai.")
    if section == "notifications" and values.get("delivery_mode") not in (None, "push", "pull"):
        raise SettingsError("notifications.delivery_mode måste vara push eller pull.")
    if section == "import" and values.get("default_coverage") not in (None, "selection", "complete", "unknown"):
        raise SettingsError("import.default_coverage måste vara selection, complete eller unknown.")


def get_section(db: Session, user_id: uuid.UUID, section: str) -> dict[str, Any]:
    if section not in DEFAULTS:
        raise SettingsError(f"Okänd sektion: {section}")
    merged = copy.deepcopy(DEFAULTS[section])
    row = db.execute(select(Setting).where(Setting.user_id == user_id, Setting.key == section)).scalar_one_or_none()
    if row and isinstance(row.value, dict):
        for k, v in row.value.items():
            if k in merged:
                if isinstance(merged[k], dict) and isinstance(v, dict) and k == "kinds":
                    merged[k].update(v)
                else:
                    merged[k] = v
    return merged


def get_all(db: Session, user_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    return {section: get_section(db, user_id, section) for section in DEFAULTS}


def update_section(db: Session, user_id: uuid.UUID, section: str, values: dict[str, Any]) -> dict[str, Any]:
    if section not in DEFAULTS:
        raise SettingsError(f"Okänd sektion: {section}")
    _validate(section, values)
    row = db.execute(select(Setting).where(Setting.user_id == user_id, Setting.key == section)).scalar_one_or_none()
    current = dict(row.value) if row and isinstance(row.value, dict) else {}
    current.update(values)
    if section == "merge":
        merged = {**DEFAULTS["merge"], **current}
        if merged["ask_max_minutes"] < merged["auto_max_minutes"]:
            raise SettingsError("Gränsen för fråga måste vara minst lika stor som gränsen för automatisk sammanslagning.")
    if section == "ai":
        merged = {**DEFAULTS["ai"], **current}
        if merged["review_threshold"] > merged["auto_threshold"]:
            raise SettingsError("Granskningströskeln får inte överstiga tröskeln för automatisk klassning.")
    if row is None:
        row = Setting(user_id=user_id, key=section, value=current)
        db.add(row)
    else:
        row.value = current
    db.flush()
    return get_section(db, user_id, section)


def user_timezone(db: Session, user_id: uuid.UUID) -> ZoneInfo:
    return ZoneInfo(get_section(db, user_id, "general")["timezone"])
