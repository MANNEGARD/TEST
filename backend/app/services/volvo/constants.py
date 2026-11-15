"""Konstanter för Volvo Cars officiella API:er (verifierade mot developer.volvocars.com 2026-11-14).

Tre publika läs-API:er används: Connected Vehicle v2, Location v1 och Energy v2.
Inget av dem lämnar ut en resejournal – resor rekonstrueras ur observationer.
"""

from __future__ import annotations

from dataclasses import dataclass

API_BASE_URL = "https://api.volvocars.com"

# Volvo ID flyttar till en ny inloggningsserver; den gamla slutar fungera 2027-01-31.
AUTH_SERVERS: dict[str, dict[str, str]] = {
    "new": {
        "authorize": "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/auth",
        "token": "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/token",
    },
    "legacy": {
        "authorize": "https://volvoid.eu.volvocars.com/as/authorization.oauth2",
        "token": "https://volvoid.eu.volvocars.com/as/token.oauth2",
    },
}
DEFAULT_AUTH_SERVER = "new"

API_CONNECTED_VEHICLE = "connected-vehicle"
API_LOCATION = "location"
API_ENERGY = "energy"

API_PREFIX = {
    API_CONNECTED_VEHICLE: "/connected-vehicle/v2",
    API_LOCATION: "/location/v1",
    API_ENERGY: "/energy/v2",
}

# Dokumenterade gränser: 100 anrop/minut per Volvo ID och klient, 10 000 anrop/dygn per API.
# Vi håller oss med marginal under båda.
REQUESTS_PER_MINUTE = 90
DAILY_BUDGET_PER_API = 9000
DOCUMENTED_DAILY_LIMIT = 10000


@dataclass(frozen=True)
class Endpoint:
    name: str
    api: str
    path: str  # relativt API-prefixet; {vin} ersätts
    scopes: tuple[str, ...]
    label: str
    polled: bool = True


ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in (
        Endpoint("odometer", API_CONNECTED_VEHICLE, "/vehicles/{vin}/odometer", ("conve:odometer_status",), "Mätarställning"),
        Endpoint("location", API_LOCATION, "/vehicles/{vin}/location", ("location:read",), "Senast rapporterad position"),
        Endpoint(
            "fuel",
            API_CONNECTED_VEHICLE,
            "/vehicles/{vin}/fuel",
            ("conve:fuel_status", "conve:battery_charge_level"),
            "Bränslenivå och batteriladdning",
        ),
        Endpoint(
            "statistics",
            API_CONNECTED_VEHICLE,
            "/vehicles/{vin}/statistics",
            ("conve:trip_statistics",),
            "Körstatistik (snittförbrukning, trippmätare, räckvidd)",
        ),
        Endpoint("engine_status", API_CONNECTED_VEHICLE, "/vehicles/{vin}/engine-status", ("conve:engine_status",), "Motorstatus"),
        Endpoint("energy_state", API_ENERGY, "/vehicles/{vin}/state", ("energy:state:read",), "Laddstatus och elräckvidd"),
        Endpoint(
            "energy_capabilities",
            API_ENERGY,
            "/vehicles/{vin}/capabilities",
            ("energy:capability:read",),
            "Energi-API:ets stöd för bilen",
            polled=False,
        ),
        Endpoint(
            "diagnostics",
            API_CONNECTED_VEHICLE,
            "/vehicles/{vin}/diagnostics",
            ("conve:diagnostics_workshop",),
            "Serviceinformation",
            polled=False,
        ),
        Endpoint(
            "command_accessibility",
            API_CONNECTED_VEHICLE,
            "/vehicles/{vin}/command-accessibility",
            ("conve:command_accessibility",),
            "Bilens uppkoppling (tillgänglighet)",
            polled=False,
        ),
    )
}

POLL_ENDPOINTS: tuple[str, ...] = tuple(name for name, e in ENDPOINTS.items() if e.polled)

VEHICLE_LIST_PATH = "/vehicles"
VEHICLE_DETAILS_PATH = "/vehicles/{vin}"
VEHICLE_RELATION_SCOPE = "conve:vehicle_relation"

# Endast läsbehörigheter – DriveLog skickar aldrig kommandon till bilen.
DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    VEHICLE_RELATION_SCOPE,
    "conve:odometer_status",
    "conve:fuel_status",
    "conve:battery_charge_level",
    "conve:trip_statistics",
    "conve:engine_status",
    "conve:diagnostics_workshop",
    "conve:command_accessibility",
    "location:read",
    "energy:state:read",
    "energy:capability:read",
)

MILES_TO_KM = 1.609344

CAP_SUPPORTED = "supported"
CAP_UNSUPPORTED = "unsupported"
CAP_NO_DATA = "no_data"
CAP_UNKNOWN = "unknown"
REPROBE_UNSUPPORTED_HOURS = 24
# Antal 403/404-svar i rad innan en tidigare fungerande endpoint räknas som ej stödd.
UNSUPPORTED_CONFIRMATIONS = 3

MODE_OAUTH = "oauth"
MODE_TEST_TOKEN = "test_token"  # noqa: S105 - lägesnamn, ingen hemlighet
MODE_DEMO = "demo"
