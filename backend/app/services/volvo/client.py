"""HTTP-klient för Volvo Cars API:er.

* Klientsidans rate limiting: glidande minutfönster (under 100 anrop/minut) och en
  dygnsbudget per API som lagras på anslutningen.
* HTTP 429 → inget omedelbart nytt försök; anroparen sätter ``backoff_until``.
* 5xx och nätverksfel → begränsat antal nya försök med exponentiell backoff + jitter.
* 401 → en tokenförnyelse och ett nytt försök.
* 403/404 för en enskild endpoint betyder att just den här bilen/behörigheten saknar
  stöd – det är ett kapabilitetsbesked, inte ett fel.

Tokens, API-nyckel och VIN skrivs aldrig till loggen.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.services.volvo import constants as C

logger = logging.getLogger(__name__)

_VIN_IN_PATH = re.compile(r"(/vehicles/)([A-HJ-NPR-Z0-9]{5,})([A-HJ-NPR-Z0-9]{4})(?=[/?\s\"']|$)", re.IGNORECASE)


class _RedactVinFilter(logging.Filter):
    """httpx loggar hela URL:en (inklusive VIN) på INFO-nivå. VIN maskeras innan loggning."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - felaktig loggpost, släpp igenom oförändrad
            return True
        if "/vehicles/" in message:
            record.msg = _VIN_IN_PATH.sub(lambda m: f"{m.group(1)}…{m.group(3)}", message)
            record.args = ()
        return True


def _install_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactVinFilter) for f in target.filters):
            target.addFilter(_RedactVinFilter())


_install_log_redaction()

# Injiceras av tester (httpx.MockTransport). None = riktig nätverkstrafik.
HTTP_TRANSPORT: httpx.BaseTransport | None = None
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_RETRIES = 2
RETRY_BASE_SECONDS = 1.0

_sleep: Callable[[float], None] = time.sleep
_rng = random.Random()  # noqa: S311 - jitter, inte kryptografi


class VolvoError(Exception):
    """Fel mot Volvo som kan visas för användaren (svenskt meddelande)."""

    def __init__(self, message: str, *, code: str = "volvo_error", http_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class VolvoAuthError(VolvoError):
    """Behörigheten har upphört – användaren måste ansluta igen."""

    def __init__(self, message: str = "Volvo-behörigheten har upphört. Anslut igen.", http_status: int | None = None):
        super().__init__(message, code="needs_reauth", http_status=http_status)


class VolvoThrottled(VolvoError):
    def __init__(self, retry_after: float | None = None, *, local: bool = False):
        super().__init__(
            "Volvos anropsgräns är nådd; nya försök görs senare." if not local
            else "Dygnsbudgeten för Volvo-anrop är förbrukad; insamlingen pausas till nästa dygn.",
            code="throttled",
            http_status=429,
        )
        self.retry_after = retry_after
        self.local = local


@dataclass
class ApiResult:
    """Utfall av ett läsanrop."""

    status: str  # ok | unsupported | no_data | throttled | error
    http_status: int | None = None
    data: Any = None
    message: str | None = None
    retry_after: float | None = None
    operation_id: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class QuotaTracker:
    """Dygnsräknare per API (UTC-dygn). Serialiseras till anslutningens capabilities."""

    date: str = ""
    calls: dict[str, int] = field(default_factory=dict)
    budget: int = C.DAILY_BUDGET_PER_API

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, budget: int = C.DAILY_BUDGET_PER_API) -> QuotaTracker:
        data = data or {}
        return cls(date=str(data.get("date") or ""), calls=dict(data.get("calls") or {}), budget=budget)

    def _roll(self, now: datetime) -> None:
        today = now.astimezone(UTC).strftime("%Y-%m-%d")
        if today != self.date:
            self.date, self.calls = today, {}

    def allow(self, api: str, now: datetime) -> bool:
        self._roll(now)
        return self.calls.get(api, 0) < self.budget

    def record(self, api: str, now: datetime) -> None:
        self._roll(now)
        self.calls[api] = self.calls.get(api, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "calls": dict(self.calls),
            "budget_per_api": self.budget,
            "documented_limit_per_api": C.DOCUMENTED_DAILY_LIMIT,
        }


class MinuteLimiter:
    """Glidande 60-sekundersfönster. Väntar hellre än att överskrida gränsen."""

    def __init__(self, per_minute: int = C.REQUESTS_PER_MINUTE, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self._clock = clock
        self._stamps: deque[float] = deque()

    def wait_time(self) -> float:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= 60.0:
            self._stamps.popleft()
        if len(self._stamps) < self.per_minute:
            return 0.0
        return max(0.0, 60.0 - (now - self._stamps[0]))

    def acquire(self) -> float:
        waited = self.wait_time()
        if waited > 0:
            _sleep(waited)
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= 60.0:
            self._stamps.popleft()
        self._stamps.append(now)
        return waited


_limiters: dict[str, MinuteLimiter] = {}


def limiter_for(key: str) -> MinuteLimiter:
    """Processgemensam minutbegränsare per anslutning (gränsen gäller per Volvo ID och klient)."""
    limiter = _limiters.get(key)
    if limiter is None:
        limiter = _limiters[key] = MinuteLimiter()
    return limiter


def backoff_seconds(failures: int, *, base: float = 60.0, cap: float = 3600.0, retry_after: float | None = None) -> float:
    """Exponentiell backoff med jitter. Retry-After respekteras när servern anger den."""
    if retry_after is not None and retry_after > 0:
        return min(cap, retry_after) + _rng.uniform(0, 5)
    delay = min(cap, base * (2 ** max(0, failures)))
    return delay + _rng.uniform(0, delay * 0.25)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(value)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


def _error_message(resp: httpx.Response) -> str:
    """Plockar ut ett kort felmeddelande ur de tre API:ernas olika felformat."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("description")
            if msg:
                return str(msg)[:300]
        if isinstance(body.get("message"), str):
            return body["message"][:300]
    return f"HTTP {resp.status_code}"


def mask_vin(vin: str | None) -> str:
    if not vin:
        return "okänt fordon"
    return f"…{vin[-4:]}" if len(vin) > 4 else "…"


class VolvoClient:
    """Synkron klient för läsanrop. En instans per insamlingskörning."""

    is_demo = False

    def __init__(
        self,
        *,
        access_token: str,
        api_key: str,
        on_unauthorized: Callable[[str], str | None] | None = None,
        quota: QuotaTracker | None = None,
        limiter: MinuteLimiter | None = None,
        base_url: str = C.API_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._token = access_token
        self._api_key = api_key
        self._on_unauthorized = on_unauthorized
        self.quota = quota or QuotaTracker()
        self.limiter = limiter or MinuteLimiter()
        self._now = now
        self._http = httpx.Client(
            base_url=base_url,
            timeout=DEFAULT_TIMEOUT,
            transport=transport if transport is not None else HTTP_TRANSPORT,
            headers={"accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> VolvoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ lågnivå

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}", "vcc-api-key": self._api_key}

    def get(self, api: str, path: str) -> ApiResult:
        """GET mot ett av API:erna. Kastar VolvoAuthError om behörigheten är förbrukad."""
        url = C.API_PREFIX[api] + path
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            if not self.quota.allow(api, self._now()):
                return ApiResult("throttled", None, message="Dygnsbudgeten är förbrukad.", attempts=attempt)
            self.limiter.acquire()
            self.quota.record(api, self._now())
            try:
                resp = self._http.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempt <= MAX_RETRIES:
                    _sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)) + _rng.uniform(0, 0.5))
                    continue
                return ApiResult("error", None, message=f"Nätverksfel: {type(exc).__name__}", attempts=attempt)

            op_id = resp.headers.get("vcc-api-operationid")
            status = resp.status_code
            if status == 200:
                try:
                    return ApiResult("ok", 200, data=resp.json(), operation_id=op_id, attempts=attempt)
                except ValueError:
                    return ApiResult("error", 200, message="Svaret var inte JSON.", operation_id=op_id, attempts=attempt)
            if status == 401:
                if not refreshed and self._on_unauthorized is not None:
                    refreshed = True
                    new_token = self._on_unauthorized(self._token)  # kan kasta VolvoAuthError
                    if new_token and new_token != self._token:
                        self._token = new_token
                        continue
                raise VolvoAuthError(http_status=401)
            if status == 429:
                return ApiResult(
                    "throttled",
                    429,
                    message="Anropsgränsen är nådd.",
                    retry_after=_parse_retry_after(resp.headers.get("retry-after")),
                    operation_id=op_id,
                    attempts=attempt,
                )
            if status in (403, 404):
                # 403 = behörighet/region saknas, 404 = resursen finns inte för bilen.
                return ApiResult(
                    "no_data" if (status == 404 and api == C.API_LOCATION) else "unsupported",
                    status,
                    message=_error_message(resp),
                    operation_id=op_id,
                    attempts=attempt,
                )
            if status >= 500 and attempt <= MAX_RETRIES:
                _sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)) + _rng.uniform(0, 0.5))
                continue
            return ApiResult("error", status, message=_error_message(resp), operation_id=op_id, attempts=attempt)

    # ------------------------------------------------------------------ högnivå

    def fetch(self, endpoint_name: str, vin: str) -> ApiResult:
        ep = C.ENDPOINTS[endpoint_name]
        return self.get(ep.api, ep.path.format(vin=vin))

    def list_vehicles(self) -> list[str]:
        result = self.get(C.API_CONNECTED_VEHICLE, C.VEHICLE_LIST_PATH)
        if result.status == "throttled":
            raise VolvoThrottled(result.retry_after)
        if not result.ok:
            raise VolvoError(
                f"Kunde inte hämta fordonslistan från Volvo ({result.message or result.status}).",
                http_status=result.http_status,
            )
        data = result.data.get("data") if isinstance(result.data, dict) else None
        vins: list[str] = []
        for item in data or []:
            vin = item.get("vin") if isinstance(item, dict) else None
            if isinstance(vin, str) and vin:
                vins.append(vin)
        return vins

    def vehicle_details(self, vin: str) -> ApiResult:
        return self.get(C.API_CONNECTED_VEHICLE, C.VEHICLE_DETAILS_PATH.format(vin=vin))


# ---------------------------------------------------------------------- tolkning


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_number(value: Any) -> float | None:
    """Volvo skickar värden som tal eller som sträng beroende på bil och endpoint."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def cv_field(payload: Any, key: str) -> tuple[Any, str | None, datetime | None]:
    """Connected Vehicle: {"data": {key: {"value", "unit", "timestamp"}}} → (värde, enhet, tid)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get(key) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return None, None, None
    return item.get("value"), item.get("unit"), parse_timestamp(item.get("timestamp"))


def energy_field(payload: Any, key: str) -> tuple[Any, str | None, datetime | None]:
    """Energy v2: {key: {"status": "OK", "value", "unit", "updatedAt"}} → (värde, enhet, tid)."""
    item = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("status") != "OK":
        return None, None, None
    return item.get("value"), item.get("unit"), parse_timestamp(item.get("updatedAt"))


def to_km(value: Any, unit: str | None) -> float | None:
    num = parse_number(value)
    if num is None:
        return None
    u = (unit or "km").lower()
    if u in ("mi", "mile", "miles"):
        return round(num * C.MILES_TO_KM, 1)
    if u in ("m", "meter", "meters", "metre", "metres"):
        return round(num / 1000.0, 1)
    return num


def strip_identifiers(value: Any) -> Any:
    """Tar bort VIN ur råsvar innan de lagras."""
    if isinstance(value, dict):
        return {k: strip_identifiers(v) for k, v in value.items() if k.lower() != "vin"}
    if isinstance(value, list):
        return [strip_identifiers(v) for v in value]
    return value
