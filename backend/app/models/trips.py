"""Fordonsobservationer, fysiska resor (RawTrip) och journalresor (LogicalTrip).

Tre nivåer hålls strikt isär:

* ``VehicleObservation`` – originaluppgifter från fordons-API:t.
* ``RawTrip``            – en individuellt identifierad eller importerad fysisk resa.
* ``LogicalTrip``        – posten som visas i körjournalen; består av ett eller flera
                           ``TripSegment`` som pekar på RawTrips.

Originalposter raderas aldrig vid sammanslagning; de makuleras enbart (``voided_at``)
när en import ångras.
"""

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


class VehicleObservation(Base):
    """En rådatapost från fordons-API:t. Både hämtningstid och fordonets egen tid sparas."""

    __tablename__ = "vehicle_observations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="volvo_api")
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # När systemet hämtade svaret (första och senaste gången identiskt innehåll sågs).
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)

    odometer_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    fuel_liters: Mapped[float | None] = mapped_column(Float, nullable=True)  # tanknivå, inte förbrukning
    fuel_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    battery_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    electric_range_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel_range_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    charging_status: Mapped[str | None] = mapped_column(String(60), nullable=True)

    engine_running: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    engine_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    avg_fuel_consumption: Mapped[float | None] = mapped_column(Float, nullable=True)  # l/100 km (bilens snitt)
    avg_energy_consumption: Mapped[float | None] = mapped_column(Float, nullable=True)  # kWh/100 km
    avg_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    trip_meter_auto_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    trip_meter_manual_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Bilens snittfart sedan trippmätaren nollställdes (averageSpeedAutomatic) och när
    # statistiken hämtades (bilen ger ingen egen tidsstämpel för den).
    avg_speed_auto: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Snittförbrukningen för samma period som tripMeterAutomatic (averageFuelConsumptionAutomatic
    # och averageEnergyConsumptionAutomatic, 1.3.0) – trippmätarmetoden i volvo/consumption.py.
    avg_fuel_consumption_auto: Mapped[float | None] = mapped_column(Float, nullable=True)  # l/100 km
    avg_energy_consumption_auto: Mapped[float | None] = mapped_column(Float, nullable=True)  # kWh/100 km
    statistics_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # "Bilen används" (command-accessibility, 1.5.0): bilens availabilityStatus och
    # unavailableReason som de kom (t.ex. AVAILABLE, UNAVAILABLE + CAR_IN_USE), bilens egen
    # tidsstämpel för statusen och det härledda car_in_use (sant bara för UNAVAILABLE med
    # CAR_IN_USE; None när ingen status lästes). Hämtningstiden för övergången är fetched_at.
    accessibility_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    accessibility_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    accessibility_reported_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    car_in_use: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    errors: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # Bearbetningsstatus i resedetekteringen
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_obs_vehicle_fetched", "vehicle_id", "fetched_at"),)


class VehicleOdometerSnapshot(Base):
    """Känd mätarställning vid en tidpunkt (API, import, manuell, årsskifte)."""

    __tablename__ = "vehicle_odometer_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    odometer_km: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30))  # api | import | manual | year_start | year_end
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="system")

    __table_args__ = (UniqueConstraint("vehicle_id", "at", "source", name="uq_odo_snapshot"),)


class RawTrip(Base):
    """En fysisk resa i originalskick."""

    __tablename__ = "raw_trips"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    # Naturlig nyckel för dubblettskydd (importerade rader / detekterade resor).
    source_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    import_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_rows.id", ondelete="SET NULL"), nullable=True
    )
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    start_time_quality: Mapped[str] = mapped_column(String(20), default="exact")
    end_time_quality: Mapped[str] = mapped_column(String(20), default="exact")
    # Gränser när faktisk starttid saknas (hittas aldrig på).
    start_earliest: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    start_latest: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Grunden för en uträknad starttid (start_time.estimate): metod, gränser och marginal.
    start_estimate_basis: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    # Sorteringsnyckel: faktisk start, annars bästa kända gräns.
    sort_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)

    odometer_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_source: Mapped[str] = mapped_column(String(20), default="export")  # export | api | user | none

    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    distance_quality: Mapped[str] = mapped_column(String(20), default="measured")
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # körtid enligt källan

    start_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    end_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_source: Mapped[str] = mapped_column(String(20), default="none")
    start_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("known_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    end_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("known_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    fuel_liters: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel_quality: Mapped[str] = mapped_column(String(20), default="unknown")
    energy_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy_quality: Mapped[str] = mapped_column(String(20), default="unknown")
    regen_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Hur en uppskattad förbrukning räknades fram (1.5.0, volvo.consumption): metod, värdena före
    # och efter, felgränsen och anteckningar. Tom för äldre resor och värden som inte är uträknade.
    fuel_basis: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    energy_basis: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    # Exakt bevarade originaluppgifter från källan.
    original_category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    original_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    original_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    possible_missed_trips: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_flags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    observation_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    # Uppehåll i hämtningen före resan (flaggan collection_gap, 1.3.0):
    # {"from": ISO, "to": ISO, "odometer_km": float | None}. Tom för äldre resor.
    collection_gap: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    voided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")

    __table_args__ = (
        Index("ix_raw_vehicle_sort", "vehicle_id", "sort_time"),
        Index("ix_raw_vehicle_key", "vehicle_id", "source_key"),
    )

    @property
    def is_void(self) -> bool:
        return self.voided_at is not None


class BusinessJourney(Base):
    """Överordnad tjänsteresa som kan omfatta flera körningar, hotell och hemresa."""

    __tablename__ = "business_journeys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="suggested")  # suggested | confirmed
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LogicalTrip(Base):
    """Journalresa. Sammanfattande fält är cache av ingående segment/RawTrips."""

    __tablename__ = "logical_trips"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(20), default="active", index=True)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="SET NULL"), nullable=True
    )
    merge_kind: Mapped[str] = mapped_column(String(20), default="none")
    journey_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("business_journeys.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- Beslut (aktuellt läge; historiken finns i TripClassification/TripPurpose) ---
    category: Mapped[str] = mapped_column(String(20), default="undecided", index=True)
    subcategory: Mapped[str | None] = mapped_column(String(40), nullable=True)
    category_source: Mapped[str] = mapped_column(String(20), default="system")
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    purpose_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(20), default="needs_review", index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_mixed_categories: Mapped[bool] = mapped_column(Boolean, default=False)
    contacts: Mapped[str | None] = mapped_column(Text, nullable=True)  # besökta företag/kontaktpersoner
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # övriga anteckningar
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    # Starttid som ägaren angett själv; vinner över bilens och den uträknade.
    start_time_override: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Vem eller vad som godkände resan (enums.ApprovalSource); NULL = ägaren (före 1.1.0).
    approval_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # {"pattern_id", "related_trip_id", "reason", "veto_until"} för automatiska godkännanden.
    approval_basis: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    auto_approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # --- Cache (räknas om av trips.segments.refresh_logical) ---
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sort_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    start_time_quality: Mapped[str] = mapped_column(String(20), default="exact")
    end_time_quality: Mapped[str] = mapped_column(String(20), default="exact")
    odometer_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    work_km: Mapped[float] = mapped_column(Float, default=0.0)
    private_km: Mapped[float] = mapped_column(Float, default=0.0)
    undecided_km: Mapped[float] = mapped_column(Float, default=0.0)
    driving_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    end_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    start_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    end_location_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    via: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)  # mellanstopp
    fuel_liters: Mapped[float | None] = mapped_column(Float, nullable=True)
    fuel_quality: Mapped[str] = mapped_column(String(20), default="unknown")
    energy_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy_quality: Mapped[str] = mapped_column(String(20), default="unknown")
    regen_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    segment_count: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="volvo_import")  # dominerande datakälla
    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    has_gap: Mapped[bool] = mapped_column(Boolean, default=False)  # odometerlucka mellan delresor
    gap_km: Mapped[float] = mapped_column(Float, default=0.0)
    quality_flags: Mapped[list[str]] = mapped_column(JSONType, default=list)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")

    segments: Mapped[list[TripSegment]] = relationship(
        back_populates="logical_trip",
        order_by="TripSegment.seq",
        cascade="all, delete-orphan",
    )

    __table_args__ = (Index("ix_logical_vehicle_state_sort", "vehicle_id", "state", "sort_time"),)


class TripSegment(Base):
    """Koppling LogicalTrip → RawTrip. En RawTrip kan delas i flera delsträckor
    (t.ex. 30 km tjänst + 12 km privat); summan måste motsvara RawTrip.distance_km."""

    __tablename__ = "trip_segments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    logical_trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), index=True
    )
    raw_trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_trips.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    part: Mapped[int] = mapped_column(Integer, default=0)  # delnummer inom samma RawTrip
    distance_km: Mapped[float] = mapped_column(Float)
    category_override: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    logical_trip: Mapped[LogicalTrip] = relationship(back_populates="segments")
    raw_trip: Mapped[RawTrip] = relationship()

    __table_args__ = (
        UniqueConstraint("logical_trip_id", "raw_trip_id", "part", name="uq_segment_logical_raw_part"),
    )


class TripMerge(Base):
    """Logg över sammanslagningar/uppdelningar; möjliggör ångra."""

    __tablename__ = "trip_merges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(20))  # merge | split
    trigger: Mapped[str] = mapped_column(String(20))  # rule_a | rule_b | manual | ha
    result_trip_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    source_trip_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    undone_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MergeDecision(Base):
    """Användarens beslut att INTE slå samman två på varandra följande resor
    (så att samma fråga inte ställs igen)."""

    __tablename__ = "merge_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    first_raw_trip_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    second_raw_trip_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    decision: Mapped[str] = mapped_column(String(20))  # keep_separate | merged | unmerged
    decided_by: Mapped[str] = mapped_column(String(64), default="user")
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("first_raw_trip_id", "second_raw_trip_id", name="uq_merge_decision_pair"),
    )


class TripClassification(Base):
    """Varje förslag eller beslut om kategori. AI-förslag skiljs från användarbeslut via source."""

    __tablename__ = "trip_classifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    logical_trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(20))
    subcategory: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    source_quality: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(60), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    signals: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="system")


class TripPurpose(Base):
    """Varje föreslaget eller beslutat syfte, inklusive motivering."""

    __tablename__ = "trip_purposes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    logical_trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    based_on: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="system")


class TripEvidence(Base):
    """Mätbara signaler som låg till grund för ett förslag."""

    __tablename__ = "trip_evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    logical_trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), index=True
    )
    classification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trip_classifications.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(300))
    direction: Mapped[str] = mapped_column(String(20))  # work | private | neutral
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    ref_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DataGap(Base):
    """Lucka i journalen: kilometer eller tid som saknar redovisad resa."""

    __tablename__ = "data_gaps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(120))
    odometer_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    odometer_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    km: Mapped[float] = mapped_column(Float, default=0.0)
    from_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    to_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    before_raw_trip_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    after_raw_trip_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_raw_trip_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("vehicle_id", "dedupe_key", name="uq_gap_vehicle_key"),)


class ReviewItem(Base):
    """Ärende i granskningscentret."""

    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160))
    logical_trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("logical_trips.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    __table_args__ = (UniqueConstraint("vehicle_id", "dedupe_key", name="uq_review_vehicle_key"),)
