"""Gemensamma svarsmodeller för REST-API:t."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    detail: str


class VehicleOut(ORM):
    id: uuid.UUID
    name: str
    make: str
    model: str
    model_year: int | None
    fuel_type: str
    vin_masked: str | None = None
    registration_number: str | None
    is_demo: bool
    is_active: bool
    is_default: bool
    capabilities: dict[str, Any] = {}


class LocationBrief(ORM):
    id: uuid.UUID
    name: str
    type: str
    city: str | None
    confirmed: bool
    latitude: float | None = None
    longitude: float | None = None


class TripOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    state: str
    category: str
    subcategory: str | None
    category_source: str
    purpose: str | None
    purpose_source: str | None
    purpose_confirmed: bool
    review_status: str
    confidence: float | None
    tax_note: str | None
    has_mixed_categories: bool
    contacts: str | None
    notes: str | None
    is_favorite: bool
    merge_kind: str
    journey_id: uuid.UUID | None
    started_at: datetime | None
    ended_at: datetime | None
    sort_time: datetime | None
    start_time_quality: str
    end_time_quality: str
    odometer_start: float | None
    odometer_end: float | None
    distance_km: float
    work_km: float
    private_km: float
    undecided_km: float
    driving_seconds: int | None
    start_address: str | None
    end_address: str | None
    start_location_id: uuid.UUID | None
    end_location_id: uuid.UUID | None
    via: list[dict[str, Any]]
    fuel_liters: float | None
    fuel_quality: str
    energy_kwh: float | None
    energy_quality: str
    regen_kwh: float | None
    segment_count: int
    source: str
    is_complete: bool
    has_gap: bool
    gap_km: float
    quality_flags: list[str]
    updated_at: datetime
    updated_by: str
    start_location: LocationBrief | None = None
    end_location: LocationBrief | None = None
    open_review_kinds: list[str] = []
    has_calendar_match: bool = False


class TripListOut(BaseModel):
    items: list[TripOut]
    total: int
    page: int
    page_size: int
    totals: dict[str, float]


class ReviewItemOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    kind: str
    title: str
    detail: str | None
    data: dict[str, Any]
    status: str
    logical_trip_id: uuid.UUID | None
    snoozed_until: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    resolution: dict[str, Any]
    trip: TripOut | None = None


class GapOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    kind: str
    odometer_start: float | None
    odometer_end: float | None
    km: float
    from_time: datetime | None
    to_time: datetime | None
    status: str
    resolution_note: str | None
    resolved_at: datetime | None


class LocationOut(ORM):
    id: uuid.UUID
    name: str
    type: str
    address: str | None
    city: str | None
    company: str | None
    latitude: float | None
    longitude: float | None
    radius_m: int
    coordinate_source: str | None
    confirmed: bool
    is_demo: bool
    suggested_type: str | None
    suggested_name: str | None
    suggestion_reason: str | None
    notes: str | None
    visit_count: int
    last_visited_at: datetime | None
    created_from: str
    aliases: list[str] = []
    stats: dict[str, Any] = {}


class PatternOut(ORM):
    id: uuid.UUID
    pattern_type: str
    origin_location_id: uuid.UUID | None
    destination_location_id: uuid.UUID
    confirmed_work: int
    confirmed_private: int
    imported_work: int
    imported_private: int
    corrections_to_private: int
    corrections_to_work: int
    purposes: list[dict[str, Any]]
    weekday_hist: dict[str, int]
    hour_hist: dict[str, int]
    last_seen_at: datetime | None
    status: str
    is_manual_rule: bool
    manual_category: str | None
    manual_purpose: str | None
    origin_name: str | None = None
    destination_name: str | None = None


class FeedbackOut(ORM):
    id: uuid.UUID
    logical_trip_id: uuid.UUID
    channel: str
    kind: str
    original_category: str | None
    new_category: str | None
    original_purpose: str | None
    new_purpose: str | None
    weekday: int | None
    local_hour: int | None
    calendar_context: dict[str, Any]
    model_assessment: dict[str, Any]
    created_at: datetime
    undone_at: datetime | None


class AuditOut(ORM):
    id: uuid.UUID
    at: datetime
    actor_type: str
    actor_id: str | None
    action: str
    entity_type: str
    entity_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class ImportBatchOut(ORM):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    status: str
    coverage: str
    summary: dict[str, Any]
    created_at: datetime
    committed_at: datetime | None
    undone_at: datetime | None


class ImportRowOut(ORM):
    id: uuid.UUID
    file_id: uuid.UUID
    row_number: int
    raw: dict[str, Any]
    parsed: dict[str, Any]
    status: str
    messages: list[str]
    raw_trip_id: uuid.UUID | None
