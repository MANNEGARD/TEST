"""Åtgärds-id:n och Home Assistant-payloads för interaktiva notiser.

iOS-appen skickar bara ``action``, ``reply_text`` och ``action_data`` i händelsen
``mobile_app_notification_action`` – varken tag eller enhet. Frågans publika id
bäddas därför in i åtgärdssträngen (``DRIVELOG_<ÅTGÄRD>_<id>``) och skickas dessutom
i ``action_data``.
"""

from __future__ import annotations

import re
from typing import Any

PREFIX = "DRIVELOG_"
TAG_PREFIX = "drivelog-"
GROUP = "drivelog"

# Åtgärdsnycklar
WORK = "WORK"
PRIVATE = "PRIVATE"
REVIEW = "REVIEW"
OPEN = "OPEN"
LATER = "LATER"
MERGE_YES = "MERGEYES"
MERGE_NO = "MERGENO"
PURPOSE_TEXT = "PURPOSETEXT"
PURPOSE_PICK = "PURPOSE"  # följs av index: PURPOSE0, PURPOSE1, PURPOSE2
TEST_OK = "TESTOK"

# Självgående läge (byggkontraktet C.4): kategori och ärende i samma tryck.
WORK_PICK = "WORKP"  # följs av index: WORKP0 = "Tjänst: {förslag}"
WORK_ALWAYS = "WORKA"  # WORKA0 = "Alltid: {förslag}" (sparar en egen regel)
WORK_TEXT = "WORKTEXT"  # "Tjänst – skriv ärende" (text)
PRIVATE_ALWAYS = "PRIVALWAYS"  # "Alltid privat här"
PURPOSE_ALWAYS = "PURPOSEA"  # PURPOSEA0 = "Alltid: {förslag}"
COMPANY_TEXT = "COMPANYTEXT"  # "Vilket företag var det?" (text)
OK = "OK"  # "Stämmer" på ett antagande
OPEN_SECOND = "OPEN1"  # andra länkknappen, t.ex. "Se antagna"

MAX_ACTIONS = 6
EXPORT_LINK_PREFIX = "export-link:"  # översätts till en signerad länk när notisen läses

NAVIGATION_ACTIONS = {REVIEW, OPEN, OPEN_SECOND}

_KEY_RE = re.compile(r"^[A-Z]+[0-9]?$")
_ID_RE = re.compile(r"^[a-z0-9]{6,24}$")


def action_id(key: str, public_id: str) -> str:
    return f"{PREFIX}{key}_{public_id}"


def parse_action_id(value: str | None) -> tuple[str, str] | None:
    """``DRIVELOG_WORK_ab12…`` → ("WORK", "ab12…"). Okända strängar ger None."""
    if not value or not value.startswith(PREFIX):
        return None
    rest = value[len(PREFIX) :]
    key, sep, public_id = rest.rpartition("_")
    if not sep or not _KEY_RE.match(key) or not _ID_RE.match(public_id):
        return None
    return key, public_id


def normalize_action(value: str) -> str:
    """Tar emot antingen en ren nyckel ("work") eller ett helt åtgärds-id."""
    parsed = parse_action_id(value)
    if parsed is not None:
        return parsed[0]
    return value.strip().upper()


def tag_for(public_id: str) -> str:
    return f"{TAG_PREFIX}{public_id}"


def absolute_url(base_url: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def build_notify_data(
    *,
    public_id: str,
    kind: str,
    title: str,
    message: str,
    actions: list[dict[str, Any]],
    base_url: str,
    open_path: str,
    reminder: bool = False,
) -> dict[str, Any]:
    """Tjänstedata för ``notify.mobile_app_<enhet>`` (Companion-appens format)."""
    ha_actions: list[dict[str, Any]] = []
    for a in actions:
        item: dict[str, Any] = {"action": action_id(a["key"], public_id), "title": a["title"]}
        if a.get("uri"):
            item["uri"] = absolute_url(base_url, a["uri"])
        if a.get("text_input"):
            item["behavior"] = "textInput"
            item["textInputButtonTitle"] = "Spara"
            item["textInputPlaceholder"] = a.get("placeholder", "Syfte med resan")
        if a.get("icon"):
            item["icon"] = a["icon"]
        ha_actions.append(item)
    return {
        "title": ("Påminnelse: " + title) if reminder else title,
        "message": message,
        "data": {
            "tag": tag_for(public_id),
            "group": GROUP,
            "url": absolute_url(base_url, open_path),
            "push": {"interruption-level": "active"},
            "action_data": {"drivelog_id": public_id, "kind": kind},
            "actions": ha_actions,
        },
    }


def build_clear_data(public_id: str) -> dict[str, Any]:
    """Tar bort en tidigare notis från telefonen (samma tag)."""
    return {"message": "clear_notification", "data": {"tag": tag_for(public_id)}}
