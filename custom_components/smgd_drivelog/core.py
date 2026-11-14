"""Ren hjälplogik för SMGD DriveLog-integrationen.

Modulen importerar ingenting från Home Assistant så att den kan enhetstestas
fristående (se backend/tests/test_ha_integration_core.py). Här finns:

* tolkning av åtgärds-id:n från ``mobile_app_notification_action``,
* bygget av svar, kvittenser och livstecken till DriveLogs API,
* den persistenta svarskön med omförsök och exponentiell backoff.

Sökvägarna nedan måste stämma exakt med backend/app/api/routes/ha.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ----------------------------------------------------------------------------- API

API_PREFIX = "/api/v1"
PATH_WHOAMI = f"{API_PREFIX}/auth/whoami"
PATH_SUMMARY = f"{API_PREFIX}/ha/summary"
PATH_OUTBOX = f"{API_PREFIX}/ha/outbox"
PATH_OUTBOX_ACK = f"{API_PREFIX}/ha/outbox/ack"
PATH_HEARTBEAT = f"{API_PREFIX}/ha/heartbeat"
PATH_CALLBACK = f"{API_PREFIX}/ha/callback"

REQUIRED_SCOPE = "ha"

# ----------------------------------------------------------------------------- åtgärds-id

ACTION_PREFIX = "DRIVELOG_"
_KEY_RE = re.compile(r"^[A-Z]+[0-9]?$")
_ID_RE = re.compile(r"^[a-z0-9]{6,24}$")
_SERVICE_RE = re.compile(r"^mobile_app_[a-z0-9_]{1,200}$")

MAX_REPLY_TEXT = 2000
MAX_ACK_ITEMS = 100


def parse_action_id(value: Any) -> tuple[str, str] | None:
    """``DRIVELOG_WORK_ab12cd34`` → ("WORK", "ab12cd34"). Allt annat ger None."""
    if not isinstance(value, str) or not value.startswith(ACTION_PREFIX):
        return None
    key, sep, public_id = value[len(ACTION_PREFIX) :].rpartition("_")
    if not sep or not _KEY_RE.match(key) or not _ID_RE.match(public_id):
        return None
    return key, public_id


def is_drivelog_action(event_data: Any) -> bool:
    return isinstance(event_data, dict) and parse_action_id(event_data.get("action")) is not None


def build_callback_payload(event_data: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    """Bygger kroppen till ``POST /ha/callback`` ur en ``mobile_app_notification_action``.

    iOS skickar bara ``action``, ``reply_text`` och ``action_data``. Frågans id läses
    ur åtgärdssträngen; ``action_data.drivelog_id`` används som kontroll. Händelser
    som inte kommer från DriveLog ger None och ska ignoreras.
    """
    if not isinstance(event_data, dict):
        return None
    parsed = parse_action_id(event_data.get("action"))
    if parsed is None:
        return None
    _, public_id = parsed
    action_data = event_data.get("action_data")
    if isinstance(action_data, dict):
        other = action_data.get("drivelog_id")
        if isinstance(other, str) and other and other != public_id:
            return None  # motsägelsefull händelse – skicka hellre inget än fel svar
    if not isinstance(event_id, str) or not 8 <= len(event_id) <= 100:
        raise ValueError("event_id måste vara 8–100 tecken.")
    payload: dict[str, Any] = {"public_id": public_id, "action": event_data["action"], "event_id": event_id}
    reply = event_data.get("reply_text")
    if isinstance(reply, str) and reply.strip():
        payload["reply_text"] = reply[:MAX_REPLY_TEXT]
    return payload


# ----------------------------------------------------------------------------- konfiguration


def normalize_base_url(url: str) -> str:
    """Normaliserar DriveLogs adress. Tar bort avslutande snedstreck och ``/api/v1``."""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("invalid_url")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("invalid_url")
    path = parts.path.rstrip("/")
    if path.endswith(API_PREFIX):
        path = path[: -len(API_PREFIX)]
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))


def api_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def notify_service_name(value: Any) -> str | None:
    """``notify.mobile_app_iphone`` → ``mobile_app_iphone``; ogiltigt ger None."""
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if name.startswith("notify."):
        name = name[len("notify.") :]
    return name if _SERVICE_RE.match(name) else None


def mobile_app_services(names: Any) -> list[str]:
    """Filtrerar fram Companion-appens legacy-tjänster ``notify.mobile_app_*``."""
    out = {n for n in (notify_service_name(x) for x in (names or [])) if n}
    return sorted(out)


def select_service(item_service: Any, *fallbacks: Any) -> str | None:
    """Tjänsten en notis ska skickas med: DriveLogs val, annars integrationens."""
    for candidate in (item_service, *fallbacks):
        name = notify_service_name(candidate)
        if name:
            return name
    return None


def token_has_scope(whoami: Any) -> bool:
    if not isinstance(whoami, dict):
        return False
    scopes = whoami.get("scopes") or []
    return isinstance(scopes, list) and (REQUIRED_SCOPE in scopes or "*" in scopes)


def build_heartbeat(
    *,
    ha_version: str | None,
    integration_version: str,
    services: list[str],
    selected: str | None,
    queue_length: int,
) -> dict[str, Any]:
    return {
        "ha_version": (ha_version or None) and str(ha_version)[:40],
        "integration_version": integration_version[:40],
        "notify_services": mobile_app_services(services)[:100],
        "notify_service": notify_service_name(selected),
        "queue_length": max(0, int(queue_length)),
    }


# ----------------------------------------------------------------------------- utkorg (pull)


@dataclass
class OutboxDelivery:
    """En notis ur utkorgen, klar att skickas med ``notify.<service>``."""

    id: str
    type: str  # notify | clear
    service: str | None
    data: dict[str, Any]


def parse_outbox(body: Any, fallback_service: str | None) -> tuple[list[OutboxDelivery], list[dict[str, str]]]:
    """Tolkar svaret från ``GET /ha/outbox``.

    Returnerar (leveranser, misslyckade) där misslyckade är poster som inte kan
    levereras (t.ex. ingen notify-tjänst vald) och ska rapporteras i kvittensen.
    """
    deliveries: list[OutboxDelivery] = []
    failed: list[dict[str, str]] = []
    items = body.get("items") if isinstance(body, dict) else None
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item_id = raw.get("id")
        data = raw.get("data")
        if not isinstance(item_id, str) or not item_id or not isinstance(data, dict):
            continue
        kind = raw.get("type") if raw.get("type") in ("notify", "clear") else "notify"
        service = select_service(raw.get("service"), fallback_service)
        if service is None:
            failed.append({"id": item_id, "error": "Ingen notify.mobile_app-tjänst är vald i DriveLog eller i integrationen."})
            continue
        if not isinstance(data.get("message"), str):
            failed.append({"id": item_id, "error": "Notisen saknar text."})
            continue
        deliveries.append(OutboxDelivery(id=item_id, type=kind, service=service, data=data))
    return deliveries, failed


def build_ack(delivered: list[str], failed: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Kvittenser till ``POST /ha/outbox/ack`` – uppdelade i omgångar om högst 100."""
    failed = [{"id": f["id"][:24], "error": (f.get("error") or "")[:300]} for f in failed]
    delivered = [d[:24] for d in delivered]
    batches: list[dict[str, Any]] = []
    while delivered or failed:
        batches.append({"delivered": delivered[:MAX_ACK_ITEMS], "failed": failed[:MAX_ACK_ITEMS]})
        delivered, failed = delivered[MAX_ACK_ITEMS:], failed[MAX_ACK_ITEMS:]
    return batches


def feedback_message(result: Any) -> str | None:
    """Text att visa på telefonen när ett svar inte gick igenom (t.ex. utgången fråga).

    Svar på frågor som DriveLog inte känner till (t.ex. en annan användares fråga när
    flera DriveLog-konton finns i samma Home Assistant) ger ingen återkoppling.
    """
    if not isinstance(result, dict) or not result.get("notify_user") or not result.get("public_id"):
        return None
    message = result.get("message")
    return message if isinstance(message, str) and message else None


# ----------------------------------------------------------------------------- svarskö

MAX_QUEUE_ITEMS = 500
MAX_QUEUE_AGE_SECONDS = 14 * 24 * 3600
BACKOFF_BASE_SECONDS = 15
BACKOFF_MAX_SECONDS = 30 * 60


def backoff_seconds(attempts: int) -> float:
    """15, 30, 60 … sekunder, högst 30 minuter."""
    if attempts <= 0:
        return 0.0
    return float(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)))


@dataclass
class QueuedResponse:
    event_id: str
    payload: dict[str, Any]
    created: float
    attempts: int = 0
    next_attempt: float = 0.0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "payload": self.payload,
            "created": self.created,
            "attempts": self.attempts,
            "next_attempt": self.next_attempt,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedResponse | None:
        try:
            payload = data["payload"]
            if not isinstance(payload, dict) or not isinstance(data["event_id"], str):
                return None
            return cls(
                event_id=data["event_id"],
                payload=payload,
                created=float(data.get("created", 0.0)),
                attempts=int(data.get("attempts", 0)),
                next_attempt=float(data.get("next_attempt", 0.0)),
                last_error=data.get("last_error"),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class ResponseQueue:
    """Svar som ännu inte nått DriveLog. Sparas på disk av integrationen (Store).

    Inget svar tappas när DriveLog är nere: det ligger kvar och försöks igen med
    växande intervall. Samma händelse läggs aldrig in två gånger, och DriveLog
    behandlar dessutom ``event_id`` som idempotensnyckel.
    """

    items: list[QueuedResponse] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    def add(self, payload: dict[str, Any], now: float) -> bool:
        event_id = payload["event_id"]
        if any(i.event_id == event_id for i in self.items):
            return False
        self.items.append(QueuedResponse(event_id=event_id, payload=payload, created=now, next_attempt=now))
        if len(self.items) > MAX_QUEUE_ITEMS:
            del self.items[: len(self.items) - MAX_QUEUE_ITEMS]
        return True

    def due(self, now: float) -> list[QueuedResponse]:
        return [i for i in self.items if i.next_attempt <= now]

    def next_due(self) -> float | None:
        return min((i.next_attempt for i in self.items), default=None)

    def succeeded(self, event_id: str) -> None:
        self.items = [i for i in self.items if i.event_id != event_id]

    def drop(self, event_id: str) -> None:
        self.succeeded(event_id)

    def failed(self, event_id: str, now: float, error: str) -> None:
        for item in self.items:
            if item.event_id == event_id:
                item.attempts += 1
                item.next_attempt = now + backoff_seconds(item.attempts)
                item.last_error = error[:300]

    def retry_all_now(self, now: float) -> None:
        for item in self.items:
            item.next_attempt = now

    def prune(self, now: float) -> int:
        """Tar bort svar som är för gamla för att vara meningsfulla (frågan har gått ut)."""
        before = len(self.items)
        self.items = [i for i in self.items if now - i.created <= MAX_QUEUE_AGE_SECONDS]
        return before - len(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, data: Any) -> ResponseQueue:
        queue = cls()
        if isinstance(data, dict):
            for raw in data.get("items") or []:
                item = QueuedResponse.from_dict(raw) if isinstance(raw, dict) else None
                if item is not None:
                    queue.items.append(item)
        return queue

    def diagnostics(self) -> list[dict[str, Any]]:
        """Kövy utan svarstexter (de kan innehålla personliga uppgifter)."""
        out: list[dict[str, Any]] = []
        for i in self.items:
            parsed = parse_action_id(i.payload.get("action"))
            out.append(
                {
                    "action": parsed[0] if parsed else None,
                    "attempts": i.attempts,
                    "has_reply_text": bool(i.payload.get("reply_text")),
                    "last_error": i.last_error,
                }
            )
        return out


# ----------------------------------------------------------------------------- fel


def classify_status(status: int) -> str:
    """HTTP-status → ``ok`` | ``auth`` | ``retry`` | ``reject``.

    ``reject`` betyder att DriveLog avvisade förfrågan som ogiltig – ett nytt försök
    med samma innehåll hjälper inte.
    """
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "auth"
    if status in (408, 425, 429) or status >= 500:
        return "retry"
    return "reject"
