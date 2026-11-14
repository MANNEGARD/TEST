/**
 * Typer för inställningssidan (/settings). Skrivna mot backendens faktiska svar:
 *   backend/app/services/settings.py        (DEFAULTS, gränsvärden)
 *   backend/app/api/routes/settings.py      (tjänsteställen, radera historik, säkerhetskopior)
 *   backend/app/api/routes/vehicles.py      (mätarställningar)
 *   backend/app/api/routes/auth.py          (API-tokens)
 */

/** Alla inställningar som { sektion: { nyckel: värde } } – precis som GET /settings svarar. */
export type SettingsMap = Record<string, Record<string, unknown>>;

export interface SettingsPayload {
  settings: SettingsMap;
  defaults: SettingsMap;
}

/** Ändringar att spara, grupperade per sektion (en PATCH /settings/{sektion} per nyckel här). */
export type SettingsChanges = Record<string, Record<string, unknown>>;

// ----------------------------------------------------------------------------- fordon

export interface VehiclePatch {
  name?: string;
  make?: string;
  model?: string;
  model_year?: number | null;
  registration_number?: string | null;
}

/** routes/vehicles.py → DriverOut */
export interface Driver {
  id: string;
  name: string;
  is_default: boolean;
}

/** routes/vehicles.py → SnapshotOut. `source`: api | import | manual | year_start | year_end */
export interface OdometerSnapshot {
  id: string;
  at: string;
  odometer_km: number;
  source: string;
  note: string;
}

export type OdometerKind = "manual" | "year_start" | "year_end";

export interface OdometerInput {
  at: string;
  odometer_km: number;
  kind: OdometerKind;
  note: string;
}

// ----------------------------------------------------------------------------- tjänsteställen

/** routes/settings.py → _workplace_out() */
export interface Workplace {
  id: string;
  kind: "location" | "home";
  location_id: string | null;
  location_name: string | null;
  valid_from: string | null;
  valid_to: string | null;
  note: string;
}

export interface WorkplaceInput {
  kind: "location" | "home";
  location_id?: string | null;
  valid_from?: string | null;
  valid_to?: string | null;
  note?: string;
}

/** Det inställningssidan behöver av en plats (GET /locations → LocationOut). */
export interface LocationOption {
  id: string;
  name: string;
  type: string;
  city: string | null;
  confirmed: boolean;
}

// ----------------------------------------------------------------------------- säkerhet

/** routes/auth.py → TokenOut. Själva hemligheten finns bara i svaret på POST (TokenCreated). */
export interface ApiToken {
  id: string;
  name: string;
  token_prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ApiTokenCreated extends ApiToken {
  token: string;
}

// ----------------------------------------------------------------------------- säkerhetskopior

/** services/backup.py → list_backups() */
export interface BackupFile {
  name: string;
  size_bytes: number;
  created_at: string;
}

// ----------------------------------------------------------------------------- radera historik

/** services/privacy.py → SCOPES */
export type DeleteHistoryScope = "observations" | "calendar" | "exports" | "notifications" | "trips";

export interface DeleteHistoryInput {
  scope: DeleteHistoryScope;
  vehicle_id?: string;
  confirm: string;
}

export interface DeleteHistoryResult {
  removed: Record<string, number>;
}
