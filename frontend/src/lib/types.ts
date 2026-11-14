/**
 * Domäntyper för frontend.
 *
 * - Typer som backend beskriver med Pydantic-modeller hämtas ur den genererade
 *   `api-types.ts` (kör `npm run gen:api` efter att backend ändrats).
 * - Endpoints som svarar med `dict[str, Any]` (resedetalj, dashboard, granskning m.fl.)
 *   har handskrivna typer här, skrivna mot backendens faktiska returvärden.
 *   Hänvisning till källfilen står vid varje typ.
 */

import type { components } from "./api-types";

type S = components["schemas"];

export type Trip = S["TripOut"];
export type TripList = S["TripListOut"];
export type TripUpdate = S["TripUpdate"];
export type BulkUpdate = S["BulkUpdate"];
export type ManualTrip = S["ManualTrip"];
export type Vehicle = S["VehicleOut"];
export type LocationBrief = S["LocationBrief"];
export type Gap = S["GapOut"];
export type ReviewItemBase = S["ReviewItemOut"];
export type Feedback = S["FeedbackOut"];
export type AuditEvent = S["AuditOut"];
export type SessionOut = S["SessionOut"];
export type User = S["UserOut"];
export type ImportBatch = S["ImportBatchOut"];

export type CategoryCode = "work" | "private" | "undecided";
export type ReviewStatusCode = "needs_review" | "proposed" | "auto" | "approved" | "imported";
export type TripSourceCode = "volvo_import" | "volvo_api" | "manual" | "gap_fill" | "demo";
export type ValueQualityCode = "verified" | "estimated" | "unknown";
export type TimeQualityCode = "exact" | "reported" | "estimated" | "bounded" | "unknown";

// ----------------------------------------------------------------------------- resedetalj
// backend/app/api/routes/trips.py → trip_detail()

export interface TripSegment {
  id: string;
  raw_trip_id: string;
  seq: number;
  part: number;
  distance_km: number;
  category: CategoryCode;
  category_override: string | null;
  note: string | null;
}

export interface RawTrip {
  id: string;
  source: TripSourceCode;
  started_at: string | null;
  ended_at: string | null;
  start_time_quality: TimeQualityCode;
  end_time_quality: TimeQualityCode;
  start_earliest: string | null;
  start_latest: string | null;
  odometer_start: number | null;
  odometer_end: number | null;
  odometer_source: string | null;
  distance_km: number;
  distance_quality: string;
  duration_seconds: number | null;
  start_address: string | null;
  end_address: string | null;
  start_position: { lat: number; lon: number } | null;
  end_position: { lat: number; lon: number } | null;
  position_source: string;
  fuel_liters: number | null;
  fuel_quality: ValueQualityCode;
  energy_kwh: number | null;
  energy_quality: ValueQualityCode;
  regen_kwh: number | null;
  original_category: string | null;
  original_title: string | null;
  original_notes: string | null;
  is_complete: boolean;
  possible_missed_trips: boolean | null;
  quality_flags: string[];
  import_origin: { file: string | null; row_number: number; format: string | null; batch_id: string | null } | null;
}

export interface MapStop {
  address: string | null;
  location: LocationBrief | null;
  lat: number | null;
  lon: number | null;
  coordinate_basis: "vehicle_position" | "known_location" | null;
  at: string | null;
}

export interface MapObservation {
  lat: number;
  lon: number;
  reported_at: string | null;
  fetched_at: string;
  odometer_km: number | null;
}

export interface Evidence {
  kind: string;
  label: string;
  direction: "work" | "private" | "neutral" | string;
  weight: number;
  ref_type: string | null;
  ref_id: string | null;
  data: Record<string, unknown>;
}

export interface Assessment {
  category: string;
  category_code: string;
  suggested_category: string | null;
  confidence: number;
  purpose: string | null;
  purpose_rationale: string | null;
  evidence: string[];
  requires_review: boolean;
  review_status: string;
  source_quality: "verified" | "limited";
  caps: string[];
  source: string;
  model: string | null;
}

export interface CalendarMatch {
  id: string;
  event_id: string;
  subject: string | null;
  start_at: string | null;
  end_at: string | null;
  location: string | null;
  is_online: boolean | null;
  relation: string | null;
  score: number;
  time_score: number | null;
  geo_score: number | null;
  geo_basis: string | null;
  is_selected: boolean;
  is_conflict: boolean;
  decided_by: string | null;
}

export interface MergeOperation {
  id: string;
  operation: string;
  trigger: string;
  created_at: string;
  created_by: string | null;
  undone_at: string | null;
  details: Record<string, unknown>;
}

export interface PurposeSuggestion {
  text: string;
  count: number;
  user_count: number;
  type: string | null;
}

export interface TripDetail {
  trip: Trip;
  segments: TripSegment[];
  raw_trips: RawTrip[];
  map: {
    stops: MapStop[];
    observations: MapObservation[];
    route_is_actual: boolean;
    route_note: string;
  };
  assessment: Assessment | null;
  assessment_meta: {
    source: string;
    model: string | null;
    created_at: string;
    confidence: number | null;
    engine_version: string | null;
  } | null;
  decision: { source: string; category: string; created_at: string; created_by: string | null } | null;
  evidence: Evidence[];
  purpose_history: {
    text: string;
    source: string;
    rationale: string | null;
    needs_confirmation: boolean;
    is_current: boolean;
    created_at: string;
  }[];
  purpose_suggestions: PurposeSuggestion[];
  calendar_matches: CalendarMatch[];
  similar_trips: Trip[];
  merged_from: Trip[];
  merge_operations: MergeOperation[];
  journey: {
    id: string;
    name: string;
    status: string;
    purpose: string | null;
    started_at: string | null;
    ended_at: string | null;
  } | null;
  history: AuditEvent[];
  feedback: Feedback[];
}

// ----------------------------------------------------------------------------- granskning
// backend/app/api/routes/review.py → list_items()

export type ReviewKindCode =
  | "category"
  | "purpose"
  | "merge"
  | "missing_odometer"
  | "data_gap"
  | "calendar_conflict"
  | "import_problem"
  | "tax_uncertain"
  | "mixed_allocation"
  | "anomaly";

export interface ReviewItem extends Omit<ReviewItemBase, "data" | "kind"> {
  kind: ReviewKindCode;
  data: Record<string, unknown> & {
    first_trip_id?: string;
    second_trip_id?: string;
    minutes_low?: number;
    minutes_high?: number;
    exact?: boolean;
  };
  kind_label: string;
  first_trip?: Trip | null;
  second_trip?: Trip | null;
}

export interface ReviewList {
  items: ReviewItem[];
  open_counts: Record<ReviewKindCode, number>;
  open_total: number;
  kind_labels: Record<string, string>;
}

export interface GapList {
  items: Gap[];
  open_count: number;
  open_km: number;
  resolved_private_km: number;
}

// ----------------------------------------------------------------------------- dashboard
// backend/app/api/routes/dashboard.py + backend/app/services/stats/dashboard.py

export type PeriodKey = "today" | "week" | "month" | "prev_month" | "year" | "all" | "custom";

export interface PeriodOut {
  key: PeriodKey;
  start: string | null;
  end: string | null;
  label: string;
  timezone: string;
}

export interface KmMil {
  km: number;
  mil: number;
}

export interface EnergyTotals {
  unit: string;
  verified: number;
  verified_km: number;
  estimated: number;
  estimated_km: number;
  unknown_km: number;
  coverage: number | null;
  per_100km: number | null;
}

export interface OdometerReading {
  value: number | null;
  at: string | null;
  source: string | null;
  source_label: string;
  is_boundary: boolean;
}

export interface DashboardSummary {
  vehicle: { id: string; name: string; registration_number: string | null; is_demo: boolean };
  period: PeriodOut;
  distance: {
    total: KmMil;
    work: KmMil;
    private: KmMil;
    undecided: KmMil;
    preliminary_work: KmMil;
    preliminary_private: KmMil;
    summarized_gap_private: KmMil;
  };
  unjournaled: { open_gap_km: number; open_gap_mil: number | null; open_gap_count: number; note: string };
  trips: {
    total: number;
    by_category: { work: number; private: number; undecided: number; mixed: number };
    preliminary: number;
    needs_review: number;
    driving_hours: number;
  };
  fuel: EnergyTotals;
  electricity: EnergyTotals & { regenerated_kwh: number };
  costs: {
    currency: string;
    fuel: { recorded: number | null; recorded_refuels: number; estimated: number | null; estimate_basis: string | null };
    electricity: {
      recorded: number | null;
      recorded_sessions: number;
      estimated: number | null;
      estimate_basis: string | null;
    };
    refuels: { count: number; liters: number; detected: number };
    charging_sessions: { count: number; kwh: number };
    recorded_total: number | null;
  };
  coverage: {
    status: "complete" | "selection" | "incomplete" | "no_data" | "unknown";
    label: string;
    explanation: string;
    reconciled: boolean;
    open_gap_count: number;
    open_gap_km: number;
    journal_km: number;
    odometer_start: OdometerReading;
    odometer_end: OdometerReading;
    odometer_delta_km: number | null;
    unreconciled_km: number | null;
    first_trip_odometer: number | null;
    last_trip_odometer: number | null;
    trips_missing_odometer: number;
    import_coverages: string[];
    private_total_derivable: boolean;
  };
  review: { total: number; by_kind: { kind: string; label: string; count: number }[] };
  odometer: OdometerReading;
  position: {
    latitude: number;
    longitude: number;
    reported_at: string | null;
    fetched_at: string | null;
    place: { id: string; name: string; type: string; distance_m: number } | null;
    source: string;
  } | null;
  sync: {
    provider: string;
    status: string;
    last_success_at: string | null;
    last_sync_at: string | null;
    last_error: string | null;
  };
}

export interface TimeseriesPoint {
  bucket: string;
  label: string;
  trips: number;
  total_km: number;
  work_km: number;
  private_km: number;
  undecided_km: number;
  preliminary_km: number;
  fuel_liters: number;
  fuel_km_covered: number;
  fuel_per_100km: number | null;
  energy_kwh: number;
  energy_km_covered: number;
  energy_per_100km: number | null;
  regen_kwh: number;
}

export interface Timeseries {
  bucket: "day" | "week" | "month";
  timezone: string;
  points: TimeseriesPoint[];
  truncated: boolean;
  period: PeriodOut;
}

export interface DestinationRow {
  location_id: string | null;
  label: string;
  address: string | null;
  type: string;
  trips: number;
  total_km: number;
  work_km: number;
  private_km: number;
  undecided_km: number;
  last_visit: string | null;
}

export interface RouteRow {
  origin: string;
  destination: string;
  trips: number;
  total_km: number;
  work_km: number;
  private_km: number;
  average_km: number;
  categories: Record<string, number>;
}

export interface MonthlyRow {
  trips: number;
  total_km: number;
  work_km: number;
  private_km: number;
  undecided_km: number;
  fuel_liters: number;
  fuel_per_100km: number | null;
  energy_kwh: number;
  energy_per_100km: number | null;
}

export interface MonthlyComparison {
  year: number;
  previous_year: number;
  months: { month: number; label: string; current: MonthlyRow; previous: MonthlyRow; total_km_change: number | null }[];
}

// ----------------------------------------------------------------------------- inställningar
// backend/app/services/settings.py → DEFAULTS (endast fält som frontend läser hittills)

export interface AppSettings {
  general: { timezone: string; driver_name: string; employer_name: string };
  privacy: {
    geocoding_enabled: boolean;
    geocoder_url: string;
    map_tiles_url: string;
    map_enabled: boolean;
    observation_retention_days: number;
  };
  [section: string]: Record<string, unknown>;
}

export interface SettingsResponse {
  settings: AppSettings;
  defaults: AppSettings;
}
