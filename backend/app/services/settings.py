"""Användarinställningar med standardvärden och validering.

Inställningarna lagras som JSON per nyckel (tabellen ``settings``). Okända nycklar
avvisas. Standardvärdena följer specifikationen (15/60 minuter osv.) men allt kan
ändras i webbgränssnittet.
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

logger = logging.getLogger(__name__)

# Journalens omfång.
#   business_only – egen bil med milersättning: endast tjänsteresor redovisas. Körning
#                   mellan de redovisade resorna är privat och är varken fel eller varning.
#   complete      – fullständig körjournal (t.ex. förmånsbil): all körning ska redovisas
#                   och luckor i mätarställningen är brister.
SCOPE_BUSINESS_ONLY = "business_only"
SCOPE_COMPLETE = "complete"
JOURNAL_SCOPES = (SCOPE_BUSINESS_ONLY, SCOPE_COMPLETE)

LAYOUT_SIMPLE = "simple"
LAYOUT_DETAILED = "detailed"
EXPORT_LAYOUTS = (LAYOUT_SIMPLE, LAYOUT_DETAILED)

# Automatik (1.1.0): självgående körjournal eller "jag godkänner allt själv".
MODE_SELF_DRIVING = "self_driving"
MODE_MANUAL = "manual"
AUTOMATION_MODES = (MODE_SELF_DRIVING, MODE_MANUAL)
DEFAULT_OFFICE_HOURS: dict[str, Any] = {"days": [0, 1, 2, 3, 4], "start": "07:00", "end": "18:00"}

# Inställningar som bara appen själv sätter (inte via API:t).
READ_ONLY: frozenset[tuple[str, str]] = frozenset({("automation", "active_since"), ("system", "last_version")})

# Skatteverkets schablonbelopp för tjänsteresor med egen bil, inkomståren 2025 och 2026.
DEFAULT_RATE_PER_MIL = 25.0

DEFAULTS: dict[str, dict[str, Any]] = {
    "automation": {
        "mode": MODE_SELF_DRIVING,  # self_driving | manual
        # Tidpunkt (ISO-8601) då självgående läge började gälla. Resor från före den rörs
        # aldrig av automatiken. Sätts av autopilot.ensure_active_since, inte av ägaren.
        "active_since": "",
        "office_hours": DEFAULT_OFFICE_HOURS,  # kontorstid för platsantagandet
        "assumption_veto_hours": 24,  # 0 = "Fråga mig först"
    },
    "general": {
        "timezone": "Europe/Stockholm",
        "driver_name": "",
        "employer_name": "",
        "journal_scope": SCOPE_BUSINESS_ONLY,  # business_only | complete
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
        "car_benefit": False,  # bilförmån/förmånsbil (standard: egen bil)
        "home_is_workplace": False,  # bostaden är tjänsteställe
        "commute_rule_enabled": True,  # tillämpa regeln bostad ↔ tjänsteställe
        "require_approval_when_uncertain": True,
    },
    "notifications": {
        "enabled": True,
        # release: en notis när DriveLog har startat med en ny version (services/release.py).
        "kinds": {"classify": True, "purpose": True, "merge": True, "weekly": True, "daily": False, "release": True},
        "quiet_start": "21:00",
        "quiet_end": "07:00",
        "max_per_day": 6,
        "batch_window_minutes": 10,
        "digest_threshold": 3,  # fler än så samtidigt → en samlingsnotis
        "reminder_after_hours": 24,
        "max_reminders": 1,
        "expire_after_hours": 48,
        "snooze_minutes": 120,
        # Notiserna visar hela adressen för platser utan namn; på = inga platser på låsskärmen.
        "hide_locations_on_lock_screen": False,
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
        # Slår upp adresser för bilens stopp (bara kartcellens mittpunkt skickas).
        "geocoding_enabled": True,
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
        "layout": LAYOUT_SIMPLE,  # simple (enkelt underlag) | detailed (fullständig arbetsbok)
        "rate_per_mil": DEFAULT_RATE_PER_MIL,  # milersättning i kronor per mil
        "show_amount": True,  # visa beräknat ersättningsbelopp
        "show_via": True,  # visa mellanstopp ("via …") under målet i underlaget
    },
    "costs": {
        "fuel_price_per_liter": None,
        "electricity_price_per_kwh": None,
    },
    "system": {
        # Versionen som DriveLog senast startade med för användaren (services/release.py).
        # Sätts bara av appen; tom = ny användare eller installation (ingen notis).
        "last_version": "",
    },
    "abbreviations": {
        # Egna förkortningar som förekommer i kommentarer → fullständig ort/plats.
        "map": {"JKPG": "Jönköping", "HBG": "Helsingborg", "BLG": "Borlänge"},
    },
}

_BOUNDS: dict[tuple[str, str], tuple[float, float]] = {
    ("automation", "assumption_veto_hours"): (0, 168),
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
    ("notifications", "snooze_minutes"): (5, 7 * 24 * 60),  # samma som POST /notifications/{id}/snooze
    ("notifications", "digest_threshold"): (1, 50),
    ("volvo", "poll_interval_seconds"): (60, 24 * 3600),
    ("volvo", "settle_minutes"): (1, 240),
    ("volvo", "position_threshold_m"): (20, 5000),
    ("calendar", "min_match_score"): (0.0, 1.0),
    ("calendar", "arrival_window_before_minutes"): (0, 24 * 60),
    ("calendar", "arrival_window_after_minutes"): (0, 24 * 60),
    ("calendar", "sync_days_back"): (0, 3650),
    ("calendar", "sync_days_forward"): (0, 365),
    ("privacy", "observation_retention_days"): (0, 3650),
    ("export", "rate_per_mil"): (0, 1000),
}

# Inställningar som bara får anta vissa värden.
_CHOICES: dict[tuple[str, str], tuple[str, ...]] = {
    ("automation", "mode"): AUTOMATION_MODES,
    ("general", "journal_scope"): JOURNAL_SCOPES,
    ("export", "layout"): EXPORT_LAYOUTS,
}


# Egna förkortningar (abbreviations.map): samma gränser som formuläret i webben.
MAX_ABBREVIATIONS = 200
MAX_ABBREVIATION_LENGTH = 20
MAX_ABBREVIATION_FULL_LENGTH = 120
MAX_URL_LENGTH = 500


class SettingsError(ValueError):
    pass


def _validate_abbreviations(value: dict[str, Any]) -> None:
    if len(value) > MAX_ABBREVIATIONS:
        raise SettingsError(f"Högst {MAX_ABBREVIATIONS} förkortningar kan sparas.")
    for short, full in value.items():
        if not isinstance(short, str) or not short.strip():
            raise SettingsError("En förkortning får inte vara tom.")
        if len(short.strip()) > MAX_ABBREVIATION_LENGTH:
            raise SettingsError(f"Förkortningen {short!r} är längre än {MAX_ABBREVIATION_LENGTH} tecken.")
        if not isinstance(full, str) or not full.strip():
            raise SettingsError(f"Förkortningen {short!r} saknar betydelse.")
        if len(full.strip()) > MAX_ABBREVIATION_FULL_LENGTH:
            raise SettingsError(f"Betydelsen för {short!r} är längre än {MAX_ABBREVIATION_FULL_LENGTH} tecken.")


def _validate_url(value: str, key: str) -> None:
    """Tom text eller en http(s)-adress (platshållare som {z} tillåts)."""
    v = value.strip()
    if not v:
        return
    if len(v) > MAX_URL_LENGTH:
        raise SettingsError(f"{key} får vara högst {MAX_URL_LENGTH} tecken.")
    parsed = urlsplit(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or any(c.isspace() for c in v):
        raise SettingsError(f"{key} måste vara en adress som börjar med https:// eller http://.")


def validate_geocoder_url(value: str) -> str:
    """Kontrollerar geokodarens adress utan nätverk och returnerar den utan snedstreck på slutet.

    Bara https (port 443), ingen användarinformation, frågesträng eller fragment, högst
    500 tecken, inte ``localhost`` och ingen IP-adress som inte är global. Samma kontroll
    görs igen vid varje anrop, då även mot DNS-svaret (places.geocode.validate_runtime_url).
    """
    v = value.strip() if isinstance(value, str) else ""
    key = "privacy.geocoder_url"
    if not v:
        raise SettingsError(f"{key} saknas.")
    if len(v) > MAX_URL_LENGTH:
        raise SettingsError(f"{key} får vara högst {MAX_URL_LENGTH} tecken.")
    if any(c.isspace() or ord(c) < 32 for c in v):
        raise SettingsError(f"{key} får inte innehålla blanksteg.")
    parsed = urlsplit(v)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SettingsError(f"{key} måste börja med https://.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise SettingsError(f"{key} får inte innehålla användarnamn eller lösenord.")
    if parsed.query or parsed.fragment or "?" in v or "#" in v:
        raise SettingsError(f"{key} får inte ha frågesträng eller fragment.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SettingsError(f"{key} har ett ogiltigt portnummer.") from exc
    if port not in (None, 443):
        raise SettingsError(f"{key} måste använda port 443.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise SettingsError(f"{key} saknar värdnamn.")
    if host == "localhost" or host.endswith(".localhost"):
        raise SettingsError(f"{key} får inte peka på den egna datorn.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise SettingsError(f"{key} får inte vara en lokal eller reserverad IP-adress.")
    return v.rstrip("/")


def _validate_office_hours(value: Any) -> None:
    key = "automation.office_hours"
    if not isinstance(value, dict) or set(value) != {"days", "start", "end"}:
        raise SettingsError(f"{key} måste ha dagar, start och slut.")
    days = value["days"]
    if (
        not isinstance(days, list)
        or not days
        or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)
        or len(set(days)) != len(days)
    ):
        raise SettingsError(f"{key}: dagarna måste vara olika tal mellan 0 (måndag) och 6 (söndag).")
    _validate_time(value["start"], f"{key}.start")
    _validate_time(value["end"], f"{key}.end")
    if value["start"] >= value["end"]:
        raise SettingsError(f"{key}: starttiden måste ligga före sluttiden.")


def _validate_time(value: Any, key: str) -> None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise SettingsError(f"{key} måste anges som HH:MM.")
    hh, mm = value.split(":")
    if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        raise SettingsError(f"{key} måste anges som HH:MM.")


def _one_of(choices: tuple[str, ...]) -> str:
    return choices[0] if len(choices) == 1 else ", ".join(choices[:-1]) + " eller " + choices[-1]


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
            choices = _CHOICES.get((section, key))
            if choices and value not in choices:
                raise SettingsError(f"{section}.{key} måste vara {_one_of(choices)}.")
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
    if section == "abbreviations" and isinstance(values.get("map"), dict):
        _validate_abbreviations(values["map"])
    if section == "privacy":
        if isinstance(values.get("map_tiles_url"), str):
            _validate_url(values["map_tiles_url"], "privacy.map_tiles_url")
        if "geocoder_url" in values:
            url = values["geocoder_url"]
            if not isinstance(url, str):
                raise SettingsError("privacy.geocoder_url måste vara text.")
            if url.strip():  # tom adress = ingen geokodning
                validate_geocoder_url(url)
    if section == "automation":
        if "office_hours" in values:
            _validate_office_hours(values["office_hours"])
        if isinstance(values.get("assumption_veto_hours"), float) and not values["assumption_veto_hours"].is_integer():
            raise SettingsError("automation.assumption_veto_hours måste vara ett helt antal timmar.")
        if values.get("active_since"):
            _parse_iso(values["active_since"], "automation.active_since")
    if section == "import" and values.get("default_coverage") not in (None, "selection", "complete", "unknown"):
        raise SettingsError("import.default_coverage måste vara selection, complete eller unknown.")


def _parse_iso(value: Any, key: str) -> datetime:
    if not isinstance(value, str):
        raise SettingsError(f"{key} måste vara en tidpunkt (ISO-8601).")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SettingsError(f"{key} måste vara en tidpunkt (ISO-8601).") from exc
    if parsed.tzinfo is None:
        raise SettingsError(f"{key} måste ange tidszon.")
    return parsed


def default_timezone() -> str:
    """Standardtidszon när användaren inte valt någon: DRIVELOG_TIMEZONE, annars Europe/Stockholm."""
    from app.config import get_config

    name = (get_config().timezone or "").strip()
    if name:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Ogiltig DRIVELOG_TIMEZONE %r – använder %s.", name, DEFAULTS["general"]["timezone"])
    return DEFAULTS["general"]["timezone"]


def defaults() -> dict[str, dict[str, Any]]:
    """Standardvärden för alla sektioner (med tidszonen från miljön)."""
    out = copy.deepcopy(DEFAULTS)
    out["general"]["timezone"] = default_timezone()
    return out


def get_section(db: Session, user_id: uuid.UUID, section: str) -> dict[str, Any]:
    if section not in DEFAULTS:
        raise SettingsError(f"Okänd sektion: {section}")
    merged = copy.deepcopy(DEFAULTS[section])
    if section == "general":
        merged["timezone"] = default_timezone()
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


def update_section(
    db: Session, user_id: uuid.UUID, section: str, values: dict[str, Any], *, allow_read_only: bool = False
) -> dict[str, Any]:
    """Sparar ändrade nycklar. Skrivskyddade nycklar (READ_ONLY) kräver ``allow_read_only``,
    som bara appen själv använder (t.ex. autopilot.ensure_active_since)."""
    if section not in DEFAULTS:
        raise SettingsError(f"Okänd sektion: {section}")
    if not allow_read_only:
        locked = sorted(k for k in values if (section, k) in READ_ONLY)
        if locked:
            raise SettingsError(f"{section}.{locked[0]} sätts av DriveLog och kan inte ändras.")
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


def automation_mode(db: Session, user_id: uuid.UUID) -> str:
    """``self_driving`` eller ``manual``; ett okänt sparat värde räknas som ``manual``."""
    value = get_section(db, user_id, "automation").get("mode")
    return value if value in AUTOMATION_MODES else MODE_MANUAL


def active_since(db: Session, user_id: uuid.UUID) -> datetime | None:
    """Tidpunkten då självgående läge började gälla, eller None om den inte är satt."""
    value = get_section(db, user_id, "automation").get("active_since")
    if not value:
        return None
    try:
        return _parse_iso(value, "automation.active_since")
    except SettingsError:
        logger.warning("Ogiltig automation.active_since för användaren – automatiken väntar.")
        return None


def user_timezone(db: Session, user_id: uuid.UUID) -> ZoneInfo:
    return ZoneInfo(get_section(db, user_id, "general")["timezone"])


def journal_scope(db: Session, user_id: uuid.UUID) -> str:
    """Journalens omfång för användaren: ``business_only`` (standard) eller ``complete``."""
    value = get_section(db, user_id, "general").get("journal_scope")
    return value if value in JOURNAL_SCOPES else DEFAULTS["general"]["journal_scope"]


def is_business_only(db: Session, user_id: uuid.UUID) -> bool:
    return journal_scope(db, user_id) == SCOPE_BUSINESS_ONLY


def reimbursement_settings(db: Session, user_id: uuid.UUID) -> tuple[float, bool]:
    """(kronor per mil, visa belopp) för milersättningen."""
    export = get_section(db, user_id, "export")
    rate = export.get("rate_per_mil")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not (0 <= rate <= 1000):
        rate = DEFAULT_RATE_PER_MIL
    return float(rate), bool(export.get("show_amount", True))


def export_show_via(db: Session, user_id: uuid.UUID) -> bool:
    """Ska underlaget visa mellanstoppen ("via …") under målet?"""
    return bool(get_section(db, user_id, "export").get("show_via", True))
