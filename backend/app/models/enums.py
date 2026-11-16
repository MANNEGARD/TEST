"""Gemensamma uppräkningstyper. Lagras som strängar i databasen."""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    WORK = "work"  # Arbete (tjänsteresa)
    PRIVATE = "private"  # Privat
    UNDECIDED = "undecided"  # Ej avgjord


CATEGORY_LABELS_SV = {
    Category.WORK: "Arbete",
    Category.PRIVATE: "Privat",
    Category.UNDECIDED: "Ej avgjord",
}


class Subcategory(StrEnum):
    CUSTOMER_VISIT = "customer_visit"
    OFFICE_WORK = "office_work"
    MEETING = "meeting"
    TRAINING = "training"
    PERSONNEL = "personnel"
    OPERATIONS = "operations"
    RETURN_HOME = "return_home"
    HOTEL_TRANSFER = "hotel_transfer"
    TRANSIT = "transit"
    COMMUTE = "commute"
    PRIVATE_ERRAND = "private_errand"
    OTHER = "other"


SUBCATEGORY_LABELS_SV = {
    Subcategory.CUSTOMER_VISIT: "Kundbesök",
    Subcategory.OFFICE_WORK: "Kontorsarbete",
    Subcategory.MEETING: "Möte",
    Subcategory.TRAINING: "Utbildning",
    Subcategory.PERSONNEL: "Personalärende",
    Subcategory.OPERATIONS: "Operativ insats",
    Subcategory.RETURN_HOME: "Hemresa från tjänsteuppdrag",
    Subcategory.HOTEL_TRANSFER: "Resa mellan kontor och hotell",
    Subcategory.TRANSIT: "Förflyttning i tjänsten",
    Subcategory.COMMUTE: "Resa mellan bostad och arbetsplats",
    Subcategory.PRIVATE_ERRAND: "Privat ärende",
    Subcategory.OTHER: "Övrigt",
}


class TripSource(StrEnum):
    VOLVO_IMPORT = "volvo_import"  # Verifierad resa ur importerad Volvo-körjournal
    VOLVO_API = "volvo_api"  # Rekonstruerad ur fordonsrapporter (API)
    MANUAL = "manual"  # Inmatad av användaren
    GAP_FILL = "gap_fill"  # Användarens sammanfattning av en odometerlucka
    DEMO = "demo"  # Demonstrationsdata


TRIP_SOURCE_LABELS_SV = {
    TripSource.VOLVO_IMPORT: "Importerad Volvo-körjournal",
    TripSource.VOLVO_API: "Rekonstruerad från fordonsrapporter",
    TripSource.MANUAL: "Manuellt inmatad",
    TripSource.GAP_FILL: "Sammanfattad lucka (användare)",
    TripSource.DEMO: "Demonstrationsdata",
}


class TimeQuality(StrEnum):
    EXACT = "exact"  # Faktisk tidpunkt från källan
    REPORTED = "reported"  # Fordonets egen tidsstämpel för observationen
    ESTIMATED = "estimated"  # Uträknad ur bilens avläsningar eller uppskattad ur sträckan (märks ≈ och "ca")
    BOUNDED = "bounded"  # Endast övre/undre gräns känd
    UNKNOWN = "unknown"


class DistanceQuality(StrEnum):
    MEASURED = "measured"  # Uppmätt av bilen (exporterad resa)
    ODOMETER = "odometer"  # Härledd ur två mätarställningar
    ESTIMATED = "estimated"
    USER = "user"


class ValueQuality(StrEnum):
    VERIFIED = "verified"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class PositionSource(StrEnum):
    EXPORT_ADDRESS = "export_address"
    API_POSITION = "api_position"
    USER = "user"
    GEOCODED = "geocoded"
    NONE = "none"


class LogicalState(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"  # ingår i en sammanslagen resa (merged_into_id)
    SPLIT = "split"  # ersatt av uppdelade resor
    VOIDED = "voided"  # makulerad (t.ex. ångrad import)


class ReviewStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"  # obligatorisk granskning
    PROPOSED = "proposed"  # preliminärt förslag, väntar på godkännande
    AUTO = "auto"  # automatiskt klassad med hög säkerhet (preliminär)
    APPROVED = "approved"  # godkänd av användaren
    IMPORTED = "imported"  # källmärkt klassning från importerad körjournal


class DecisionSource(StrEnum):
    USER = "user"
    IMPORT = "import"
    RULE = "rule"
    AI = "ai"
    PATTERN = "pattern"
    HA = "ha"  # användarens svar via Home Assistant
    SYSTEM = "system"


class MergeKind(StrEnum):
    NONE = "none"
    AUTO = "auto"
    PROMPTED = "prompted"
    MANUAL = "manual"


class LocationType(StrEnum):
    HOME = "home"
    REGULAR_WORKPLACE = "regular_workplace"
    OFFICE = "office"
    CUSTOMER = "customer"
    HOTEL = "hotel"
    PRIVATE = "private"
    FUEL_STATION = "fuel_station"
    CHARGING_STATION = "charging_station"
    OTHER = "other"
    UNKNOWN = "unknown"


LOCATION_TYPE_LABELS_SV = {
    LocationType.HOME: "Hem",
    LocationType.REGULAR_WORKPLACE: "Ordinarie arbetsplats",
    LocationType.OFFICE: "Företagskontor",
    LocationType.CUSTOMER: "Kund",
    LocationType.HOTEL: "Hotell",
    LocationType.PRIVATE: "Privat destination",
    LocationType.FUEL_STATION: "Bensinstation",
    LocationType.CHARGING_STATION: "Laddstation",
    LocationType.OTHER: "Övrig",
    LocationType.UNKNOWN: "Okänd",
}


class ReviewKind(StrEnum):
    CATEGORY = "category"
    PURPOSE = "purpose"
    MERGE = "merge"
    MISSING_ODOMETER = "missing_odometer"
    DATA_GAP = "data_gap"
    CALENDAR_CONFLICT = "calendar_conflict"
    IMPORT_PROBLEM = "import_problem"
    TAX_UNCERTAIN = "tax_uncertain"
    MIXED_ALLOCATION = "mixed_allocation"
    ANOMALY = "anomaly"


REVIEW_KIND_LABELS_SV = {
    ReviewKind.CATEGORY: "Oklar kategori",
    ReviewKind.PURPOSE: "Oklart syfte",
    ReviewKind.MERGE: "Förfrågan om sammanslagning",
    ReviewKind.MISSING_ODOMETER: "Saknad mätarställning",
    ReviewKind.DATA_GAP: "Misstänkt datalucka",
    ReviewKind.CALENDAR_CONFLICT: "Konflikt mellan kalender och fordonsdata",
    ReviewKind.IMPORT_PROBLEM: "Importproblem",
    ReviewKind.TAX_UNCERTAIN: "Skattemässig bedömning krävs",
    ReviewKind.MIXED_ALLOCATION: "Fördela privat/tjänst",
    ReviewKind.ANOMALY: "Avvikelse",
}


class ReviewState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"


class IntegrationProvider(StrEnum):
    VOLVO = "volvo"
    MICROSOFT = "microsoft"
    HOME_ASSISTANT = "home_assistant"
    AI = "ai"
    GEOCODER = "geocoder"
    CALENDAR_ICS = "calendar_ics"  # hemlig kalenderlänk (ICS) utan inloggning


class IntegrationStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"  # uppgifter sparade men ingen verifierad anslutning
    DEMO = "demo"
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"


class NotificationKind(StrEnum):
    CLASSIFY = "classify"
    PURPOSE = "purpose"
    MERGE = "merge"
    DIGEST = "digest"  # sammanlagd fråga om flera ärenden
    WEEKLY = "weekly"
    DAILY = "daily"
    TEST = "test"
    ASSUMED = "assumed"  # antagen tjänsteresa (platsantagande), vetotid
    COMPANY = "company"  # "Vilket företag var det?"
    ALERT = "alert"  # larm om kopplingarna (Volvo, kalender, bilen tyst)
    MONTHLY = "monthly"  # månadsskiftet: klart eller väntar


class ApprovalSource(StrEnum):
    """Vem eller vad som godkände en resa (``LogicalTrip.approval_source``).

    NULL betyder ägarens eget beslut från före 1.1.0 och räknas som ``user``.
    """

    USER = "user"  # ägarens beslut
    BULK = "bulk"  # ägarens massgodkännande
    RULE = "rule"  # ägarens egen regel (R)
    HISTORY = "history"  # som de 3 senaste gångerna (H)
    RETURN_TRIP = "return_trip"  # hemresa efter tjänsteresa (T)
    COMMUTE = "commute"  # resa mellan bostad och arbetsplats (P)
    PRIVATE_PLACE = "private_place"  # mellan privata platser (P)
    ASSUMED = "assumed"  # antaget kundbesök (A), med vetotid


# Automatiska godkännanden: räknas aldrig som ägarbekräftad historik.
AUTO_APPROVAL_SOURCES: frozenset[str] = frozenset(
    s.value for s in ApprovalSource if s not in (ApprovalSource.USER, ApprovalSource.BULK)
)


class GeocodeStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    OFF = "off"


class PlaceKind(StrEnum):
    WORK = "work"
    NEVER = "never"
    UNCLEAR = "unclear"


class PlaceAssumption(StrEnum):
    CUSTOMER = "customer"
    REVOKED = "revoked"


class ExportOrigin(StrEnum):
    MANUAL = "manual"  # NULL räknas också som manual
    MONTH_CLOSE = "month_close"


class MonthCloseState(StrEnum):
    OPEN = "open"
    WAITING = "waiting"
    EXPORTED = "exported"
    STALE = "stale"
    EMPTY = "empty"


class NotificationStatus(StrEnum):
    PENDING = "pending"  # skapad, väntar på leveransfönster
    SENT = "sent"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ImportRowStatus(StrEnum):
    OK = "ok"
    DUPLICATE = "duplicate"
    DUPLICATE_DIFFERS = "duplicate_differs"
    ERROR = "error"
    IMPORTED = "imported"
    SKIPPED = "skipped"


class BatchStatus(StrEnum):
    PREVIEW = "preview"
    COMMITTED = "committed"
    UNDONE = "undone"
    FAILED = "failed"


class Coverage(StrEnum):
    COMPLETE = "complete"  # komplett fordonsjournal för perioden
    SELECTION = "selection"  # importerat urval (t.ex. endast tjänsteresor)
    INCOMPLETE = "incomplete"  # ofullständig period
    UNKNOWN = "unknown"


COVERAGE_LABELS_SV = {
    Coverage.COMPLETE: "Komplett fordonsjournal",
    Coverage.SELECTION: "Importerat urval",
    Coverage.INCOMPLETE: "Ofullständig period",
    Coverage.UNKNOWN: "Okänd täckning",
}


class GapKind(StrEnum):
    ODOMETER = "odometer"  # km mellan två kända resor saknar journalpost
    COLLECTION = "collection"  # avbrott i datainsamlingen
    MULTI_TRIP = "multi_trip"  # flera körningar kan ha skett mellan observationer


class GapStatus(StrEnum):
    OPEN = "open"
    RESOLVED_PRIVATE = "resolved_private"
    RESOLVED_FILLED = "resolved_filled"
    IGNORED = "ignored"


class PatternStatus(StrEnum):
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"
