/**
 * Demots tillstånd: startdata (inspelade svar från den riktiga servern) + det ägaren har
 * ändrat. Allt ligger i minnet och sparas i webbläsarens localStorage – aldrig på en server.
 */

// Inspelad JSON från servern har många former; formen styrs av api-types.ts och inspelningen.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = any;

export const STATE_KEY = "drivelog.demo.state.v1";
export const TZ = "Europe/Stockholm";
/** Id:n som demot delar ut till resor och frågor som skapas under körning (sidorna finns förbyggda). */
export const POOL_SIZE = 40;

export function poolTripId(n: number): string {
  return `00000000-0000-4000-8000-${String(n + 1).padStart(12, "0")}`;
}

/**
 * De sista id:na i uppsättningen är reserverade för demots importerade resor (1.3.0), så att
 * de har förbyggda sidor. Resor som skapas under körning får de övriga.
 */
export const IMPORTED_TRIPS = 3;

export function importedTripId(n: number): string {
  return poolTripId(POOL_SIZE - 1 - n);
}

export function poolQuestionId(n: number): string {
  return `de0000000000${(n + 1).toString(16).padStart(4, "0")}`;
}

export interface DemoState {
  version: number;
  seedNow: string;
  vehicleId: string;
  me: Json;
  vehicles: Json[];
  trips: Record<string, Json>;
  notifications: Record<string, Json>;
  notificationOrder: string[];
  inbox: Json[];
  review: Json[];
  reviewMeta: Json;
  locations: Json[];
  locationMeta: Json;
  locationMetaAfterHome: Json;
  homeFromVehicle: Json;
  homeMarked: boolean;
  settings: Json;
  monthClose: Json;
  exports: Json[];
  ha: Json;
  volvo: Json;
  importPreview: Json;
  importBatch: Json;
  importRows: Record<string, Json>;
  imports: Json[];
  linksRevokedAt: string | null;
  nextTrip: number;
  nextQuestion: number;
  /** Ägarens beslut (lärande-feedback), nyast först – grunden för "Ångra". Fylls från inspelningen. */
  feedback?: Json[];
  /** Resans läge före ett beslut, per feedback-id (så att "Ångra" kan återställa det). */
  undo?: Record<string, { detail: Json; review: Json[] }>;
  /** Tjänsteställen och mätarställningar (Inställningar), fylls från inspelningen. */
  workplaces?: Json[];
  odometer?: Json[];
  /** Tillägget för 1.3.0 (importerade resor, uppehåll i hämtningen, nya inställningar) är inlagt. */
  v130?: boolean;
  /** 1.3.0, omgång AI: underlagsnivå, lunchregeln, hela bokningen, hem ↔ kontor och ett ärende ur kalendern. */
  v130ai?: boolean;
  /** 1.3.0, omgång drift: uppskattad förbrukning på resor från bilen och notisvalet för nya versioner. */
  v130drift?: boolean;
}

type Runtime = "seedNow" | "homeMarked" | "imports" | "linksRevokedAt" | "nextTrip" | "nextQuestion";

export interface Seed extends Omit<DemoState, Runtime> {
  now: string;
  statics: Record<string, { status: number; json: Json }>;
}

export function freshState(seed: Seed): DemoState {
  const { statics: _statics, now, ...rest } = JSON.parse(JSON.stringify(seed)) as Seed;
  void _statics;
  return {
    ...rest,
    seedNow: now,
    homeMarked: false,
    imports: [],
    linksRevokedAt: null,
    nextTrip: 0,
    nextQuestion: 0,
  };
}

export function loadState(seed: Seed): DemoState {
  try {
    const raw = window.localStorage.getItem(STATE_KEY);
    if (raw) {
      const saved = JSON.parse(raw) as DemoState;
      if (saved && saved.version === seed.version && saved.seedNow === seed.now) return saved;
    }
  } catch {
    /* privat fönster eller blockerad lagring – demot fungerar ändå, men utan att minnas */
  }
  return freshState(seed);
}

export function saveState(state: DemoState): void {
  try {
    window.localStorage.setItem(STATE_KEY, JSON.stringify(state));
  } catch {
    /* fullt eller blockerat – ändringarna gäller tills sidan laddas om */
  }
}

// ----------------------------------------------------------------------------- tid

const dayFmt = new Intl.DateTimeFormat("sv-SE", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const timeFmt = new Intl.DateTimeFormat("sv-SE", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });

/** Lokalt datum "2026-11-16" för en tidpunkt. */
export function localDay(iso: string | Date): string {
  return dayFmt.format(typeof iso === "string" ? new Date(iso) : iso);
}

export function localMonth(iso: string | Date): string {
  return localDay(iso).slice(0, 7);
}

export function localTime(iso: string | Date): string {
  return timeFmt.format(typeof iso === "string" ? new Date(iso) : iso);
}

/** Demots klocka (Date är förskjuten till demots "nu" av skriptet i <head>). */
export function nowIso(): string {
  return new Date().toISOString();
}
