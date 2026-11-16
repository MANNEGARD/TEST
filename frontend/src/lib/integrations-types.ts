/**
 * Handskrivna typer för integrationsvyn. Backend beskriver svaren som dict[str, Any]:
 *   backend/app/api/routes/integrations.py  (AI)
 *   backend/app/api/routes/volvo.py         (Volvo Cars)
 *   backend/app/api/routes/calendar.py      (Microsoft Outlook, /integrations/microsoft/*,
 *                                           kalenderlänk /integrations/microsoft/ics*)
 *   backend/app/api/routes/ha.py            (Home Assistant, /integrations/home-assistant/*)
 *   backend/app/api/routes/notifications.py (demotelefonen)
 *   backend/app/api/routes/auth.py          (API-tokens)
 * Uppdatera typerna när backend ändras.
 */

export type IntegrationStatusCode = "not_configured" | "configured" | "demo" | "connected" | "needs_reauth" | "error";
export type ProviderKey = "volvo" | "microsoft" | "home_assistant" | "ai";

/** services/integrations.py → public_view(): aldrig hemligheter, bara om de finns. */
export interface IntegrationView {
  provider: string;
  status: IntegrationStatusCode;
  has_secrets: Record<string, boolean>;
  scopes: string[];
  capabilities: Record<string, unknown>;
  last_sync_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  token_expires_at: string | null;
}

// ----------------------------------------------------------------------------- Volvo

export type CapabilityState = "supported" | "unsupported" | "no_data" | "unknown";

export interface VolvoCapability {
  state: CapabilityState;
  label?: string;
  api?: string;
  http_status?: number | null;
  checked_at?: string | null;
  message?: string | null;
}

export interface VolvoEndpoint {
  name: string;
  label: string;
  api: string;
  scopes: string[];
  /** Hämtas löpande (true) eller provas bara en gång (false). */
  polled: boolean;
}

/** Ett fordon i DriveLog som är kopplat till Volvo-kontot (eller demobilen). */
export interface VolvoLinkedVehicle {
  id: string;
  name: string;
  is_demo: boolean;
  vin_masked: string | null;
  model: string | null;
  model_year: number | null;
  fuel_type: string | null;
  capabilities: Record<string, VolvoCapability>;
  energy_capabilities: Record<string, boolean>;
  probed_at: string | null;
  data_freshness: { stale?: boolean; newest_reported_at?: string | null } | null;
  last_observation_at: string | null;
}

/** En bil på Volvo-kontot (maskerat VIN, `ref` används vid val). */
export interface VolvoAccountVehicle {
  ref: string;
  vin_masked: string;
  model: string | null;
  model_year: number | string | null;
  fuel_type: string | null;
  license_plate: string | null;
}

export interface VolvoAccountVehicles {
  vehicles: VolvoAccountVehicle[];
  selected_ref: string | null;
}

export interface VolvoRun {
  kind: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  http_status: number | null;
  error: string | null;
  stats: Record<string, unknown>;
}

export interface VolvoScenario {
  key: string;
  description: string;
}

export interface VolvoStatus extends IntegrationView {
  config: { client_id?: string; mode?: string; auth_server?: string; vehicle_id?: string; vehicle_ref?: string };
  is_connected: boolean;
  mode: "oauth" | "test_token" | "demo" | null;
  test_token_mode: boolean;
  test_token_notice: string | null;
  redirect_uri: string;
  auth_server: "new" | "legacy";
  requested_scopes: string[];
  reauth_due_at: string | null;
  vehicles: VolvoLinkedVehicle[];
  stale: boolean;
  last_run: VolvoRun | null;
  endpoints: VolvoEndpoint[];
  demo: {
    enabled: boolean;
    vehicle: VolvoLinkedVehicle | null;
    last_run: VolvoRun | null;
    scenarios: VolvoScenario[];
  };
  /** Endast i svaret från select-vehicle. */
  probe_error?: string | null;
}

export interface VolvoSyncResult {
  vehicle_id: string;
  result: { status?: string; message?: string; new_trips?: number; observation?: string };
  last_observation_at: string | null;
  at: string;
}

export interface VolvoSimulateResult {
  scenario: string;
  description: string;
  polls: number;
  new_trip_ids: string[];
  vehicle_id: string;
  ended_at: string;
}

// ----------------------------------------------------------------------------- Microsoft Outlook

export interface MicrosoftStatus extends IntegrationView {
  config: { client_id?: string; tenant?: string; account?: string };
  redirect_uri: string;
  required_permissions: string[];
  selected_calendar_ids: string[];
  stored_events: number;
  stores_body_preview: boolean;
  /** Antal sparade bokningar med beskrivning respektive deltagare (1.3.0, efter synk). */
  events_with_description?: number | null;
  events_with_attendees?: number | null;
  /** Tips om kalendern (t.ex. att bokningarna saknar beskrivning). */
  tip?: string | null;
  /** Påhittade demomöten som finns kvar (nyare backend). */
  demo_events?: number;
}

export interface MicrosoftCalendar {
  id: string;
  name: string;
  is_default: boolean;
  owner: string | null;
  selected: boolean;
}

export interface MicrosoftSyncResult {
  status?: string;
  retry_at?: string;
  created?: number;
  updated?: number;
  removed?: number;
  calendars?: number;
  trips_with_match?: number;
}

// ----------------------------------------------------------------------------- Kalenderlänk (ICS)

/** routes/calendar.py → GET/PUT/DELETE /integrations/microsoft/ics. Länken lämnas aldrig ut. */
export interface IcsStatus {
  provider: "calendar_ics";
  status: IntegrationStatusCode;
  has_link: boolean;
  host: string | null;
  last_sync_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  stored_events: number;
  events_with_location: number | null;
  /** Antal sparade bokningar med beskrivning respektive deltagare (1.3.0, efter synk). */
  events_with_description?: number | null;
  events_with_attendees?: number | null;
  tip: string | null;
  stores_body_preview: boolean;
  sync_days_back: number;
  sync_days_forward: number;
}

/** routes/calendar.py → POST /integrations/microsoft/ics/sync. */
export interface IcsSyncResult {
  status: "ok";
  created: number;
  updated: number;
  removed: number;
  events: number;
  skipped: number;
  truncated: boolean;
  tip: string | null;
  trips_with_match: number;
}

// ----------------------------------------------------------------------------- Home Assistant

export type DeliveryMode = "push" | "pull";

/** services/ha/connection.py → public_status(). */
export interface HaStatus {
  status: IntegrationStatusCode;
  demo: boolean;
  delivery_mode: DeliveryMode;
  channel: "demo" | "ha_push" | "ha_pull" | null;
  base_url: string | null;
  has_token: boolean;
  notify_service: string | null;
  /** Sann när DriveLog har frågor att skicka men ingen telefon är vald (och det inte finns exakt en). */
  phone_missing?: boolean;
  ha_version: string | null;
  location_name: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  heartbeat: {
    received_at: string | null;
    ha_version: string | null;
    integration_version: string | null;
    notify_services: string[];
    queue_length: number | null;
    fresh: boolean;
  };
  last_test_sent_at: string | null;
  last_test_confirmed_at: string | null;
  open_questions?: number;
}

export interface HaConfigInput {
  base_url?: string;
  token?: string;
  clear_token?: boolean;
  notify_service?: string;
  delivery_mode?: DeliveryMode;
}

export interface HaTestResult {
  ok: boolean;
  status: IntegrationStatusCode;
  message: string;
  devices?: string[];
  ha_version?: string | null;
  location_name?: string | null;
}

export interface HaDevices {
  source: "demo" | "heartbeat" | "home_assistant";
  devices: string[];
  selected: string | null;
}

export interface HaTestNotification {
  public_id: string;
  channel: string;
  delivered: boolean;
  queued: boolean;
  demo: boolean;
  message: string;
}

export interface NotificationAction {
  key: string;
  title: string;
  text_input: boolean;
  uri: string | null;
}

export interface DemoPhoneNotification {
  public_id: string;
  kind: string;
  title: string;
  message: string;
  sent_at: string | null;
  actions: NotificationAction[];
  /** Exakt den tjänstedata som skulle ha skickats till Home Assistant. */
  ha_payload: Record<string, unknown>;
}

export interface RespondResult {
  outcome: "applied" | "duplicate" | "expired" | "invalid" | "opened" | "snoozed" | string;
  message: string;
  public_id: string | null;
  trip_id: string | null;
  follow_up: string | null;
}

export interface ApiTokenOut {
  id: string;
  name: string;
  token_prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiTokenCreated extends ApiTokenOut {
  /** Visas endast en gång. */
  token: string;
}

// ----------------------------------------------------------------------------- AI

export type AiProviderName = "none" | "anthropic" | "openai";

export interface AiStatus extends IntegrationView {
  provider_name: AiProviderName;
  model: string;
  default_models: Record<"anthropic" | "openai", string>;
  has_key: Record<"anthropic" | "openai", boolean>;
  privacy: {
    sends: string;
    never_sends: string;
    send_calendar_subjects: boolean;
    send_calendar_body: boolean;
    send_street_addresses: boolean;
    /** Underlagsnivå (1.3.0, ai.context_level). Saknas mot en äldre server = minimalt. */
    context_level?: "full" | "minimal";
  };
  rule_engine_only: boolean;
}

export interface AiTestResult extends AiStatus {
  ok: boolean;
  /** Modellnamnet som leverantören själv svarade med vid testet. */
  verified_model?: string | null;
}
