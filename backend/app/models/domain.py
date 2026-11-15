"""Platser, mönster, inlärning, kalender, notiser, import och energi."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType, UTCDateTime, new_id, utcnow

# --------------------------------------------------------------------------- platser


class KnownLocation(Base):
    __tablename__ = "known_locations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(30), default="unknown", index=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_m: Mapped[int] = mapped_column(Integer, default=150)
    coordinate_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # user | api | geocoded
    # Användaren har bekräftat namn/typ. Obekräftade platser ger bara svaga signaler.
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    suggested_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    suggestion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    visit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_visited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_from: Mapped[str] = mapped_column(String(20), default="import")
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Adressuppslag (enums.GeocodeStatus): pending | ok | not_found | failed | off.
    geocode_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    geocoded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    osm_class: Mapped[str | None] = mapped_column(String(60), nullable=True)  # t.ex. "building=warehouse"
    place_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)  # work | never | unclear
    assumption: Mapped[str | None] = mapped_column(String(20), nullable=True)  # customer | revoked
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    aliases: Mapped[list[LocationAlias]] = relationship(back_populates="location", cascade="all, delete-orphan")


class LocationAlias(Base):
    """Normaliserad adressnyckel som pekar på en känd plats."""

    __tablename__ = "location_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("known_locations.id", ondelete="CASCADE"), index=True)
    address_key: Mapped[str] = mapped_column(String(300))
    raw_address: Mapped[str] = mapped_column(String(300))

    location: Mapped[KnownLocation] = relationship(back_populates="aliases")

    __table_args__ = (UniqueConstraint("owner_user_id", "address_key", name="uq_alias_owner_key"),)


class LocationPattern(Base):
    """Aggregerat mönster för en destination eller en rutt (start → mål).

    Räknas om deterministiskt ur användarbekräftade resor och källmärkt importerad
    klassning – aldrig ur AI:ns egna obekräftade förslag (ingen självförstärkning).
    """

    __tablename__ = "location_patterns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    pattern_type: Mapped[str] = mapped_column(String(20))  # destination | route
    pattern_key: Mapped[str] = mapped_column(String(100))
    origin_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    destination_location_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)

    confirmed_work: Mapped[int] = mapped_column(Integer, default=0)  # användarbekräftat
    confirmed_private: Mapped[int] = mapped_column(Integer, default=0)
    imported_work: Mapped[int] = mapped_column(Integer, default=0)  # källmärkt import (urval!)
    imported_private: Mapped[int] = mapped_column(Integer, default=0)
    corrections_to_private: Mapped[int] = mapped_column(Integer, default=0)
    corrections_to_work: Mapped[int] = mapped_column(Integer, default=0)
    purposes: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    weekday_hist: Mapped[dict[str, int]] = mapped_column(JSONType, default=dict)
    hour_hist: Mapped[dict[str, int]] = mapped_column(JSONType, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Godkännande av inlärt mönster samt manuell regel.
    status: Mapped[str] = mapped_column(String(20), default="suggested")
    is_manual_rule: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    manual_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("owner_user_id", "pattern_key", name="uq_pattern_owner_key"),)


class LearningFeedback(Base):
    """En korrigering eller bekräftelse från användaren. Kan ångras."""

    __tablename__ = "learning_feedback"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    logical_trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20), default="web")  # web | ha | bulk
    kind: Mapped[str] = mapped_column(String(20), default="correction")  # correction | confirmation
    original_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    original_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_state: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # för ångra
    origin_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    destination_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    origin_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    destination_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calendar_context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    model_assessment: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class GeocodeCache(Base):
    """Svar från geokodaren per kartcell (lat/lon avrundat till 4 decimaler).

    Samma cell slås aldrig upp två gånger, inte heller när svaret var ``not_found``.
    """

    __tablename__ = "geocode_cache"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_host: Mapped[str] = mapped_column(String(200))
    lat_key: Mapped[int] = mapped_column(Integer)  # round(lat * 1e4)
    lon_key: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # ok | not_found | failed
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "provider_host", "lat_key", "lon_key", name="uq_geocode_cell"),
    )


# --------------------------------------------------------------------------- kalender


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("integration_connections.id", ondelete="CASCADE"), nullable=True
    )
    calendar_id: Mapped[str] = mapped_column(String(300), default="")
    external_id: Mapped[str] = mapped_column(String(400))
    ical_uid: Mapped[str | None] = mapped_column(String(400), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    is_all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    show_as: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sensitivity: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    online_provider: Mapped[str | None] = mapped_column(String(60), nullable=True)
    location_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_master_id: Mapped[str | None] = mapped_column(String(400), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "calendar_id", "external_id", name="uq_calendar_event_ext"),
    )


class CalendarMatch(Base):
    __tablename__ = "calendar_matches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    logical_trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), index=True
    )
    calendar_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calendar_events.id", ondelete="CASCADE"), index=True
    )
    relation: Mapped[str] = mapped_column(String(20))  # arrival | departure | during
    score: Mapped[float] = mapped_column(Float)
    time_score: Mapped[float] = mapped_column(Float)
    geo_score: Mapped[float] = mapped_column(Float)
    geo_basis: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    is_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_by: Mapped[str] = mapped_column(String(20), default="auto")  # auto | user
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    event: Mapped[CalendarEvent] = relationship()

    __table_args__ = (UniqueConstraint("logical_trip_id", "calendar_event_id", name="uq_calmatch_trip_event"),)


# --------------------------------------------------------------------------- notiser


class NotificationRequest(Base):
    """En fråga/notis till användaren. Kan besvaras via HA eller i webbappen."""

    __tablename__ = "notification_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    public_id: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    logical_trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), nullable=True, index=True
    )
    review_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ha_push | ha_pull | demo | web
    digest_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # ingår i samlingsnotis
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    send_after: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    responses: Mapped[list[NotificationResponse]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class NotificationResponse(Base):
    __tablename__ = "notification_responses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notification_requests.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(40))
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="ha")  # ha | web | demo
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(30), default="applied")  # applied | duplicate | expired | invalid
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    request: Mapped[NotificationRequest] = relationship(back_populates="responses")


# --------------------------------------------------------------------------- import


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="preview")
    coverage: Mapped[str] = mapped_column(String(20), default="unknown")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    files: Mapped[list[ImportedFile]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class ImportedFile(Base):
    __tablename__ = "imported_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    stored_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    format: Mapped[str] = mapped_column(String(40), default="unknown")
    sheet_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="parsed")  # parsed | duplicate_file | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    column_map: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    batch: Mapped[ImportBatch] = relationship(back_populates="files")
    rows: Mapped[list[ImportRow]] = relationship(back_populates="file", cascade="all, delete-orphan")


class ImportRow(Base):
    __tablename__ = "import_rows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("imported_files.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # originalvärden som text
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    natural_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="ok")
    messages: Mapped[list[str]] = mapped_column(JSONType, default=list)
    raw_trip_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    file: Mapped[ImportedFile] = relationship(back_populates="rows")

    __table_args__ = (Index("ix_import_row_file_row", "file_id", "row_number"),)


# --------------------------------------------------------------------------- energi


class FuelRecord(Base):
    """Tankningstillfälle (manuellt registrerat eller detekterat ur tanknivå)."""

    __tablename__ = "fuel_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    liters: Mapped[float | None] = mapped_column(Float, nullable=True)
    liters_quality: Mapped[str] = mapped_column(String(20), default="verified")
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_per_liter: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    station: Mapped[str | None] = mapped_column(String(200), nullable=True)
    full_tank: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | detected
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ChargingRecord(Base):
    __tablename__ = "charging_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    kwh_quality: Mapped[str] = mapped_column(String(20), default="verified")
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
