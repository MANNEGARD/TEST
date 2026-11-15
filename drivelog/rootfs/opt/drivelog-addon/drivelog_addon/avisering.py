"""Aviseringar i Home Assistant (persistent notification) via Supervisorns proxy.

Enligt Home Assistants utvecklardokumentation för appar (contributors.home-assistant.io,
"Communication" och "App configuration", lästa 2026-11-15): med ``homeassistant_api: true`` i
config.yaml når appen Home Assistant Cores REST-API på ``http://supervisor/core/api/`` med sin
egen token i miljövariabeln ``SUPERVISOR_TOKEN`` (``Authorization: Bearer``). En åtgärd anropas med
``POST /api/services/<domän>/<åtgärd>`` och åtgärdens data som JSON. Genom proxyn agerar appen
som administratör av Home Assistant – därför används token bara här, i startprogrammet (root),
och följer aldrig med i miljön till API, webbapp, Caddy eller create-admin (miljöerna byggs
uttryckligen i startprogram.py).

Adressen är fast; ingen del av den kommer ur inställningar eller nätverkssvar. Saknas token
(utanför Home Assistant, i CI) skickas ingenting. Fel loggas högst en gång per start och fäller
aldrig något.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable

logger = logging.getLogger("drivelog.adressvakt")

BAS = "http://supervisor/core/api/services/persistent_notification/"
NOTIS_ID = "drivelog_adressvakt"

Post = Callable[[str, bytes, dict[str, str], float], int]


def _post(url: str, data: bytes, rubriker: dict[str, str], timeout: float) -> int:
    """POST utan proxy från miljön (token ska bara till Supervisorn). Returnerar HTTP-status."""
    oppnare = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    forfragan = urllib.request.Request(url, data=data, headers=rubriker, method="POST")  # noqa: S310 - fast adress
    try:
        with oppnare.open(forfragan, timeout=timeout) as svar:
            svar.read(4096)
            return svar.status
    except urllib.error.HTTPError as exc:
        return exc.code


class Aviserare:
    """Anropas av adressvakten: ``aviserare("skapa", titel, text)`` eller ``aviserare("ta_bort", None, None)``."""

    def __init__(self, token: str | None, *, post: Post = _post) -> None:
        self._token = token or None
        self._post = post
        self._fel_loggat = False

    def __repr__(self) -> str:  # token ska aldrig hamna i en loggrad
        return f"Aviserare(token={'satt' if self._token else 'saknas'})"

    @property
    def aktiv(self) -> bool:
        return self._token is not None

    def __call__(self, atgard: str, titel: str | None, text: str | None) -> bool:
        if self._token is None:
            return False
        if atgard == "skapa":
            url, data = BAS + "create", {"notification_id": NOTIS_ID, "title": titel, "message": text}
        elif atgard == "ta_bort":
            url, data = BAS + "dismiss", {"notification_id": NOTIS_ID}
        else:
            raise ValueError(atgard)
        rubriker = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        try:
            status = self._post(url, json.dumps(data).encode("utf-8"), rubriker, 15.0)
        except Exception as exc:  # noqa: BLE001 - ett fel hos Supervisorn får aldrig fälla något
            orsak = type(exc).__name__
        else:
            if 200 <= status < 300:
                return True
            orsak = f"HTTP {status}"
        if not self._fel_loggat:
            self._fel_loggat = True
            logger.warning(
                "Aviseringen i Home Assistant kunde inte %s (%s). Loggen gäller ändå; felet visas bara en "
                "gång per start. Kontrollera att appen är uppdaterad och har startats om.",
                "skickas" if atgard == "skapa" else "tas bort",
                orsak,
            )
        return False
