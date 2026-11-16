/**
 * Simulerad DriveLog-server i webbläsaren (bara i demobygget, NEXT_PUBLIC_DRIVELOG_DEMO=1).
 *
 * Varje anrop som webbappen gör mot /api/v1 besvaras här, utan nätverk. Startdatat är riktiga
 * svar som spelats in från backenden med påhittade resor (demo-static/startdata.json). Det
 * ägaren gör (svara på frågor, godkänna resor, markera bostaden, ändra inställningar …) ändrar
 * tillståndet i minnet, och följderna räknas fram på samma sätt som servern visar dem: listor,
 * räknare, översikten och månadskortet. Det som inte simuleras svarar med ett tydligt besked.
 */

import {
  type DemoState,
  type Json,
  type Seed,
  loadState,
  localDay,
  localMonth,
  localTime,
  nowIso,
  poolQuestionId,
  poolTripId,
  POOL_SIZE,
  saveState,
} from "./state";

export const NOT_IN_DEMO = "Det här går inte att prova i demot.";
const STATIC_BASE = "/demo";
export const EXPORT_FILE = `${STATIC_BASE}/korjournal-exempel.xlsx`;
const API = "/api/v1";
/** En kort fördröjning så att laddningslägen syns som mot en riktig server. */
const LATENCY_MS = 90;

let seed: Seed | null = null;
let state: DemoState | null = null;
let booting: Promise<void> | null = null;

declare global {
  interface Window {
    /** Anrop som demot inte känner igen (läses av demots helhetstest). */
    __drivelogDemoUnhandled?: string[];
  }
}

function boot(): Promise<void> {
  booting ??= (async () => {
    const res = await window.fetch(`${STATIC_BASE}/startdata.json`, { cache: "force-cache" });
    if (!res.ok) throw new Error("Demots startdata kunde inte läsas.");
    seed = (await res.json()) as Seed;
    state = loadState(seed);
    const rec = (prefix: string) => {
      const key = Object.keys(seed!.statics).find((k) => k.split("?")[0] === prefix);
      return key ? clone(seed!.statics[key].json) : [];
    };
    state.feedback ??= rec("/learning/feedback");
    state.undo ??= {};
    state.workplaces ??= rec("/settings/workplaces/list");
    state.odometer ??= rec(`/vehicles/${state.vehicleId}/odometer`);
    // Körtiden saknas på de rekonstruerade resorna i inspelningen; räkna den ur start- och sluttid.
    // Med en uträknad starttid lämnas den tom som i den riktiga servern, så resans sida visar "≈".
    for (const d of Object.values(state.trips)) {
      const t = d.trip;
      if (
        t.driving_seconds == null &&
        t.started_at &&
        t.ended_at &&
        t.start_time_source !== "estimated" &&
        !["bounded", "unknown", "estimated"].includes(t.start_time_quality)
      ) {
        t.driving_seconds = Math.max(0, Math.round((Date.parse(t.ended_at) - Date.parse(t.started_at)) / 1000));
      }
    }
  })();
  return booting;
}

function S(): DemoState {
  if (!state) throw new Error("Demot har inte startat.");
  return state;
}

function persist(): void {
  if (state) saveState(state);
}

// ----------------------------------------------------------------------------- svar

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly code = "error",
    readonly data: Record<string, unknown> = {},
  ) {
    super(detail);
  }
}

const notInDemo = () => new HttpError(501, NOT_IN_DEMO, "demo_unavailable");
const notFound = (what: string) => new HttpError(404, `${what} hittades inte.`, "not_found");

function json(body: unknown, status = 200): Response {
  if (status === 204 || body === undefined) return new Response(null, { status: 204 });
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

// ----------------------------------------------------------------------------- resor

const AUTO_SOURCES = new Set(["rule", "history", "return_trip", "commute", "private_place", "assumed"]);
const OPEN_STATUSES = new Set(["needs_review", "proposed"]);

function trip(id: string): Json {
  const d = S().trips[id];
  if (!d) throw notFound("Resan");
  return d.trip;
}

function allTrips(): Json[] {
  return Object.values(S().trips)
    .map((d) => d.trip)
    .filter((t) => t.state === "active");
}

function splitKm(t: Json): void {
  const km = Number(t.distance_km) || 0;
  t.work_km = t.category === "work" ? km : 0;
  t.private_km = t.category === "private" ? km : 0;
  t.undecided_km = t.category === "undecided" ? km : 0;
}

const AUDIT_FIELDS = ["category", "subcategory", "category_source", "purpose", "purpose_source", "purpose_confirmed", "review_status", "confidence", "notes", "approval_source"];

/** Resans läge före ett beslut (för "Ångra" och ändringshistoriken). */
function snapshot(id: string): { detail: Json; review: Json[] } {
  const d = S().trips[id];
  const { feedback: _f, history: _h, ...rest } = d;
  void _f;
  void _h;
  return { detail: clone(rest), review: clone(S().review.filter((r) => r.trip_id === id)) };
}

/** Registrerar ägarens beslut som feedback (som backenden) och i resans ändringshistorik. */
function recordDecision(id: string, before: { detail: Json; review: Json[] }, channel = "web"): void {
  const s = S();
  const d = s.trips[id];
  const t = d.trip;
  const old = before.detail.trip;
  const changed = AUDIT_FIELDS.some((k) => JSON.stringify(old[k] ?? null) !== JSON.stringify(t[k] ?? null));
  if (!changed) return;
  const now = nowIso();
  const local = new Date(now);
  const fb = {
    id: crypto.randomUUID(),
    logical_trip_id: id,
    channel,
    kind: old.category !== t.category || (old.purpose ?? null) !== (t.purpose ?? null) ? "correction" : "confirmation",
    original_category: old.category,
    new_category: t.category,
    original_purpose: old.purpose ?? null,
    new_purpose: t.purpose ?? null,
    weekday: (local.getDay() + 6) % 7,
    local_hour: Number(localTime(now).slice(0, 2)),
    calendar_context: {},
    model_assessment: null,
    created_at: now,
    undone_at: null,
  };
  (s.feedback ??= []).unshift(fb);
  (s.undo ??= {})[fb.id] = before;
  d.feedback = [clone(fb), ...(d.feedback ?? [])];
  d.history = [
    {
      id: crypto.randomUUID(),
      at: now,
      actor_type: channel === "ha" ? "ha" : "user",
      actor_id: s.me.user.id,
      action: "trip.update",
      entity_type: "logical_trip",
      entity_id: id,
      before: Object.fromEntries(AUDIT_FIELDS.map((k) => [k, old[k] ?? null])),
      after: Object.fromEntries(AUDIT_FIELDS.map((k) => [k, t[k] ?? null])),
    },
    ...(d.history ?? []),
  ];
}

/** "Ångra": resan (och dess ärenden) tillbaka till läget före beslutet. */
function undoFeedback(fbId: string): Json {
  const s = S();
  const fb = (s.feedback ?? []).find((f) => f.id === fbId);
  if (!fb) throw notFound("Ändringen");
  if (fb.undone_at) throw new HttpError(409, "Ändringen är redan ångrad.", "already_undone");
  const d = s.trips[fb.logical_trip_id];
  if (!d) throw notFound("Resan");
  const later = (s.feedback ?? []).find((f) => f.logical_trip_id === fb.logical_trip_id && !f.undone_at && f.created_at > fb.created_at);
  if (later) throw new HttpError(409, "Resan har ändrats efter det här. Ångra den senaste ändringen först.", "later_changes");
  const now = nowIso();
  const current = clone(d.trip);
  const snap = s.undo?.[fbId];
  if (snap) {
    const keep = { feedback: d.feedback, history: d.history };
    s.trips[fb.logical_trip_id] = { ...clone(snap.detail), ...keep };
    for (const r of snap.review) {
      const i = s.review.findIndex((x) => x.id === r.id);
      if (i >= 0) s.review[i] = clone(r);
      else s.review.unshift(clone(r));
    }
    delete s.undo![fbId];
  } else {
    // Ett beslut från startdatat: tillbaka till kategorin och syftet före beslutet.
    const t = d.trip;
    t.category = fb.original_category;
    t.purpose = fb.original_purpose;
    t.purpose_confirmed = false;
    t.review_status = fb.original_category === "undecided" ? "needs_review" : "proposed";
    t.approval_source = null;
    t.approval_reason = null;
    splitKm(t);
  }
  const d2 = s.trips[fb.logical_trip_id];
  fb.undone_at = now;
  d2.feedback = (d2.feedback ?? []).map((f: Json) => (f.id === fbId ? { ...f, undone_at: now } : f));
  d2.trip.updated_at = now;
  d2.history = [
    {
      id: crypto.randomUUID(),
      at: now,
      actor_type: "user",
      actor_id: s.me.user.id,
      action: "feedback.undo",
      entity_type: "logical_trip",
      entity_id: fb.logical_trip_id,
      before: Object.fromEntries(AUDIT_FIELDS.map((k) => [k, current[k] ?? null])),
      after: Object.fromEntries(AUDIT_FIELDS.map((k) => [k, d2.trip[k] ?? null])),
    },
    ...(d2.history ?? []),
  ];
  persist();
  return clone(d2.trip);
}

function feedbackList(q: URLSearchParams): Json[] {
  const limit = Math.min(500, Number(q.get("limit") ?? 50));
  const rows = (S().feedback ?? []).filter((f) => (q.get("only_corrections") === "true" ? f.kind === "correction" : true));
  return clone(rows.slice(0, limit));
}

/** Ägarens beslut om en resa (som feedback.apply_decision i backenden). */
function decide(
  id: string,
  change: { category?: string; purpose?: string | null; approve?: boolean; notes?: string | null },
  channel = "web",
): Json {
  const d = S().trips[id];
  if (!d) throw notFound("Resan");
  const before = snapshot(id);
  const t = d.trip;
  if (change.category !== undefined) {
    t.category = change.category;
    t.category_source = "user";
    t.review_status = change.category === "undecided" ? "needs_review" : "approved";
    if (change.category !== "work") t.subcategory = null;
  }
  if (change.purpose !== undefined) {
    const text = (change.purpose ?? "").trim();
    t.purpose = text || null;
    t.purpose_source = text ? "user" : null;
    t.purpose_confirmed = !!text;
  }
  if (change.notes !== undefined) t.notes = change.notes;
  if (change.approve) {
    t.review_status = "approved";
    t.category_source = "user";
    if (t.purpose) t.purpose_confirmed = true;
  }
  if (t.category === "private" && change.purpose === undefined && !t.purpose_confirmed) {
    t.purpose = null;
    t.purpose_source = null;
  }
  if (t.review_status === "approved" && (change.category !== undefined || change.purpose !== undefined || change.approve)) {
    t.approval_source = "user";
    t.approval_reason = null;
    t.open_review_kinds = [];
  }
  splitKm(t);
  t.updated_at = nowIso();
  t.updated_by = `user:${S().me.user.id}`;
  for (const seg of d.segments ?? []) seg.category = t.category;
  if (d.decision) d.decision = { source: "user", category: t.category, created_at: t.updated_at, created_by: t.updated_by };
  if (t.review_status === "approved") closeFollowUps(id);
  recordDecision(id, before, channel);
  return t;
}

/** Ärenden och frågor som blivit inaktuella när resan avgjorts. */
function closeFollowUps(tripId: string, except?: string): void {
  const s = S();
  const now = nowIso();
  for (const item of s.review) {
    if (item.trip_id === tripId && item.status === "open") {
      item.status = "resolved";
      item.resolved_at = now;
    }
  }
  for (const pid of s.notificationOrder) {
    const n = s.notifications[pid];
    if (!n || pid === except || n.trip_id !== tripId) continue;
    if (n.status === "pending" || n.status === "sent") {
      n.status = "cancelled";
      n.answerable = false;
    }
  }
  s.inbox = s.inbox.filter((n) => s.notifications[n.public_id]?.status !== "cancelled");
  deliverNext();
}

function tripMatches(t: Json, q: URLSearchParams): boolean {
  const from = q.get("from");
  const to = q.get("to");
  const day = localDay(t.sort_time);
  if (from && day < from) return false;
  if (to && day > to) return false;
  const cats = q.getAll("category");
  if (cats.length && !cats.includes(t.category)) return false;
  const statuses = q.getAll("review_status");
  if (statuses.length && !statuses.includes(t.review_status)) return false;
  const sources = q.getAll("source");
  if (sources.length && !sources.includes(t.source)) return false;
  if (q.get("needs_review") === "true" && !OPEN_STATUSES.has(t.review_status)) return false;
  if (q.get("favorite") === "true" && !t.is_favorite) return false;
  const loc = q.get("location_id");
  if (loc && t.start_location_id !== loc && t.end_location_id !== loc) return false;
  const journey = q.get("journey_id");
  if (journey && t.journey_id !== journey) return false;
  const min = q.get("min_km");
  if (min && Number(t.distance_km) < Number(min)) return false;
  const max = q.get("max_km");
  if (max && Number(t.distance_km) > Number(max)) return false;
  const text = q.get("q")?.trim().toLowerCase();
  if (text) {
    const hay = [t.start_address, t.end_address, t.purpose, t.notes, t.start_location?.name, t.end_location?.name]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!text.split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

const SORT_KEYS: Record<string, (t: Json) => string | number | null> = {
  date: (t) => t.sort_time,
  start: (t) => t.started_at ?? t.sort_time,
  end: (t) => t.ended_at,
  distance: (t) => Number(t.distance_km),
  category: (t) => t.category,
  status: (t) => t.review_status,
  odometer: (t) => t.odometer_start,
  purpose: (t) => t.purpose,
};

function listTrips(q: URLSearchParams): Json {
  const rows = allTrips().filter((t) => tripMatches(t, q));
  const key = SORT_KEYS[q.get("sort") ?? "date"] ?? SORT_KEYS.date;
  const dir = q.get("order") === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const x = key(a);
    const y = key(b);
    if (x === y) return a.id < b.id ? -1 : 1;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : 1) * dir;
  });
  const page = Math.max(1, Number(q.get("page") ?? 1));
  const size = Math.min(500, Math.max(1, Number(q.get("page_size") ?? 50)));
  const sum = (f: (t: Json) => number) => Math.round(rows.reduce((acc, t) => acc + f(t), 0) * 10) / 10;
  return {
    items: rows.slice((page - 1) * size, page * size).map(clone),
    total: rows.length,
    page,
    page_size: size,
    totals: {
      distance_km: sum((t) => Number(t.distance_km) || 0),
      work_km: sum((t) => Number(t.work_km) || 0),
      private_km: sum((t) => Number(t.private_km) || 0),
      undecided_km: sum((t) => Number(t.undecided_km) || 0),
    },
  };
}

function tripDetail(id: string): Json {
  const d = S().trips[id];
  if (!d) throw new HttpError(404, "Resan hittades inte.", "not_found");
  const out = clone(d);
  delete out.demo_original_start;
  out.trip.open_review_kinds = S()
    .review.filter((r) => r.trip_id === id && r.status === "open")
    .map((r) => r.kind)
    .sort();
  return out;
}

function setStartTime(id: string, value: string | null): void {
  const d = S().trips[id];
  const t = trip(id);
  const keys = ["started_at", "start_time_quality", "start_time_source", "start_time_text", "start_earliest", "start_latest", "start_estimate_basis", "quality_flags"];
  if (value === null) {
    // "Ta bort den angivna starttiden": tillbaka till bilens (eller okänd) starttid.
    if (d.demo_original_start) Object.assign(t, clone(d.demo_original_start));
    delete d.demo_original_start;
    return;
  }
  d.demo_original_start ??= Object.fromEntries(keys.map((k) => [k, clone(t[k] ?? null)]));
  const at = new Date(value);
  const end = t.ended_at ? new Date(t.ended_at) : null;
  if (!end) throw new HttpError(400, "Resan saknar sluttid, så starttiden kan inte anges.");
  if (at.getTime() > end.getTime() || end.getTime() - at.getTime() > 24 * 3600_000) {
    throw new HttpError(400, "Starttiden måste ligga inom 24 timmar före resans sluttid.", "start_time_out_of_range");
  }
  const iso = at.toISOString();
  t.started_at = iso;
  t.start_time_quality = "exact";
  t.start_time_source = "user";
  t.start_time_text = localTime(iso);
  t.start_earliest = null;
  t.start_latest = null;
  t.start_estimate_basis = null;
  t.quality_flags = (t.quality_flags ?? []).filter((f: string) => f !== "start_time_uncertain");
}

/** "Lägg till resa": en manuell resa med ett id ur demots förbyggda uppsättning. */
function createManualTrip(body: Json): Json {
  const s = S();
  if (s.nextTrip >= POOL_SIZE) throw new HttpError(400, "Demot rymmer inte fler nya resor. Återställ demot för att börja om.");
  const started = new Date(body.started_at);
  const ended = new Date(body.ended_at);
  if (Number.isNaN(started.getTime()) || Number.isNaN(ended.getTime())) throw new HttpError(400, "Ange start- och sluttid.");
  if (ended < started) throw new HttpError(400, "Sluttiden ligger före starttiden.");
  if ((body.odometer_start == null) !== (body.odometer_end == null)) {
    throw new HttpError(400, "Ange både mätarställning start och slut, eller ingen av dem.");
  }
  const id = poolTripId(s.nextTrip++);
  const template = Object.values(s.trips)[0];
  const km = Math.round(Number(body.distance_km) * 1000) / 1000;
  const category = body.category ?? "undecided";
  const decided = category !== "undecided";
  const purpose = typeof body.purpose === "string" && body.purpose.trim() ? body.purpose.trim() : null;
  const now = nowIso();
  const findLoc = (address: string) =>
    s.locations.find((l) => (l.address ?? "").toLowerCase() === address.toLowerCase() || (l.aliases ?? []).includes(address));
  const brief = (l: Json) =>
    l ? { id: l.id, name: l.name, type: l.type, city: l.city, confirmed: l.confirmed, latitude: l.latitude, longitude: l.longitude } : null;
  const sLoc = findLoc(String(body.start_address).trim());
  const eLoc = findLoc(String(body.end_address).trim());
  const t = {
    ...clone(template.trip),
    id,
    state: "active",
    category,
    subcategory: null,
    category_source: decided ? "user" : "system",
    purpose,
    purpose_source: purpose ? "user" : null,
    purpose_confirmed: !!purpose,
    review_status: decided ? "approved" : "needs_review",
    confidence: null,
    notes: null,
    is_favorite: false,
    merge_kind: "none",
    journey_id: null,
    started_at: started.toISOString(),
    ended_at: ended.toISOString(),
    sort_time: started.toISOString(),
    start_time_quality: "exact",
    end_time_quality: "exact",
    start_earliest: null,
    start_latest: null,
    odometer_start: body.odometer_start ?? null,
    odometer_end: body.odometer_end ?? null,
    distance_km: km,
    driving_seconds: Math.round((ended.getTime() - started.getTime()) / 1000),
    start_address: String(body.start_address).trim(),
    end_address: String(body.end_address).trim(),
    start_location_id: sLoc?.id ?? null,
    end_location_id: eLoc?.id ?? null,
    start_location: brief(sLoc),
    end_location: brief(eLoc),
    via: [],
    fuel_liters: null,
    fuel_quality: "unknown",
    energy_kwh: null,
    energy_quality: "unknown",
    regen_kwh: null,
    segment_count: 1,
    source: "manual",
    is_complete: body.odometer_start != null,
    has_gap: false,
    gap_km: 0,
    quality_flags: ["user_entered", ...(body.odometer_start != null ? [] : ["missing_odometer"])],
    updated_at: now,
    updated_by: `user:${s.me.user.id}`,
    open_review_kinds: decided ? [] : ["category"],
    has_calendar_match: false,
    start_time_source: "user",
    start_time_text: localTime(started),
    start_estimate_basis: null,
    approval_source: decided ? "user" : null,
    approval_reason: null,
  };
  splitKm(t);
  const rawId = crypto.randomUUID();
  s.trips[id] = {
    ...clone(template),
    trip: t,
    segments: [{ id: crypto.randomUUID(), raw_trip_id: rawId, seq: 0, part: 0, distance_km: km, category, category_override: null, note: null }],
    raw_trips: [
      {
        ...clone(template.raw_trips?.[0] ?? {}),
        id: rawId,
        source: "manual",
        started_at: t.started_at,
        ended_at: t.ended_at,
        start_time_quality: "exact",
        end_time_quality: "exact",
        start_earliest: null,
        start_latest: null,
        odometer_start: t.odometer_start,
        odometer_end: t.odometer_end,
        odometer_source: t.odometer_start != null ? "user" : "none",
        distance_km: km,
        distance_quality: "user",
        duration_seconds: t.driving_seconds,
        start_address: t.start_address,
        end_address: t.end_address,
        start_position: null,
        end_position: null,
        position_source: "user",
        fuel_liters: null,
        fuel_quality: "unknown",
        energy_kwh: null,
        energy_quality: "unknown",
        regen_kwh: null,
        is_complete: t.is_complete,
        possible_missed_trips: null,
        quality_flags: t.quality_flags,
        import_origin: null,
      },
    ],
    map: { stops: [], observations: [], route_is_actual: false, route_note: null },
    assessment: null,
    assessment_meta: null,
    decision: decided ? { source: "user", category, created_at: now, created_by: t.updated_by } : null,
    evidence: [],
    purpose_history: purpose ? [{ text: purpose, source: "user", rationale: null, needs_confirmation: false, is_current: true, created_at: now }] : [],
    purpose_suggestions: [],
    calendar_matches: [],
    similar_trips: [],
    merged_from: [],
    merge_operations: [],
    journey: null,
    history: [],
    feedback: [],
  };
  if (!decided) {
    s.review.unshift({
      id: crypto.randomUUID(),
      vehicle_id: s.vehicleId,
      kind: "category",
      title: `Arbete eller privat? ${t.start_address} → ${t.end_address}`,
      detail: null,
      data: { trip_id: id },
      status: "open",
      logical_trip_id: id,
      snoozed_until: null,
      created_at: now,
      resolved_at: null,
      resolution: {},
      kind_label: s.reviewMeta.kind_labels.category,
      trip_id: id,
    });
  }
  persist();
  return clone(t);
}

// ----------------------------------------------------------------------------- granskning

function reviewList(q: URLSearchParams): Json {
  const s = S();
  const status = q.get("status") ?? "open";
  const limit = Number(q.get("limit") ?? 200);
  const items = s.review
    .filter((r) => (status === "all" ? true : status === "open" ? r.status === "open" : r.status !== "open"))
    .filter((r) => s.trips[r.trip_id])
    .slice(0, limit)
    .map(({ trip_id, ...r }) => ({ ...clone(r), trip: clone(s.trips[trip_id].trip) }));
  const counts: Record<string, number> = Object.fromEntries(Object.keys(s.reviewMeta.open_counts).map((k) => [k, 0]));
  for (const r of s.review) if (r.status === "open") counts[r.kind] = (counts[r.kind] ?? 0) + 1;
  return {
    items,
    open_counts: counts,
    open_total: Object.values(counts).reduce((a, b) => a + b, 0),
    kind_labels: s.reviewMeta.kind_labels,
  };
}

function reviewItem(id: string): Json {
  const r = S().review.find((x) => x.id === id);
  if (!r) throw notFound("Ärendet");
  return r;
}

function reviewItemOut(r: Json): Json {
  const { trip_id, ...rest } = r;
  return { ...clone(rest), trip: S().trips[trip_id] ? clone(S().trips[trip_id].trip) : null };
}

// ----------------------------------------------------------------------------- frågor (notiser)

const QUESTION_KINDS = new Set(["classify", "purpose", "company", "assumed", "merge"]);

function listItem(n: Json): Json {
  const out = clone(n);
  delete out.trip;
  delete out.merge_trips;
  delete out.summary;
  delete out.answer;
  return out;
}

function tripSummary(id: string): Json {
  const t = S().trips[id]?.trip;
  if (!t) return null;
  return {
    id: t.id,
    started_at: t.started_at,
    ended_at: t.ended_at,
    start_address: t.start_address,
    end_address: t.end_address,
    distance_km: t.distance_km,
    category: t.category,
    review_status: t.review_status,
    purpose: t.purpose,
    purpose_confirmed: t.purpose_confirmed,
    state: t.state,
    start_label: t.start_location?.name ?? t.start_address,
    end_label: t.end_location?.name ?? t.end_address,
    start_time_quality: t.start_time_quality,
    end_time_quality: t.end_time_quality,
  };
}

function notificationDetail(pid: string): Json {
  const n = S().notifications[pid];
  if (!n) throw new HttpError(404, "Frågan hittades inte.", "not_found");
  const out = clone(n);
  if (out.trip_id) out.trip = { ...(out.trip ?? {}), ...tripSummary(out.trip_id) };
  return out;
}

function phoneEntry(n: Json): Json {
  return {
    public_id: n.public_id,
    kind: n.kind,
    title: n.title,
    message: n.message,
    sent_at: n.sent_at,
    actions: clone(n.actions),
    ha_payload: {
      title: n.title,
      message: n.message,
      data: { tag: `drivelog-${n.public_id}`, group: "drivelog", url: `/m/q/${n.public_id}` },
    },
  };
}

/** En fråga i taget i telefonen: när ingen obesvarad fråga visas skickas nästa. */
function deliverNext(): void {
  const s = S();
  const open = (n: Json) => QUESTION_KINDS.has(n.kind) && n.status === "sent" && n.answerable !== false;
  const showing = s.notificationOrder.some((pid) => s.notifications[pid] && open(s.notifications[pid]) && s.notifications[pid].kind !== "assumed");
  if (!showing) {
    const now = nowIso();
    const next = s.notificationOrder
      .map((pid) => s.notifications[pid])
      .filter((n) => n && n.status === "pending" && QUESTION_KINDS.has(n.kind) && (!n.send_after || n.send_after <= now))
      .sort((a, b) => (a.created_at < b.created_at ? -1 : 1))[0];
    if (next) {
      next.status = "sent";
      next.channel = "demo";
      next.sent_at = now;
      s.inbox.unshift(phoneEntry(next));
    }
  }
  s.ha.open_questions = s.notificationOrder.filter((pid) => {
    const n = s.notifications[pid];
    return n && QUESTION_KINDS.has(n.kind) && (n.status === "pending" || n.status === "sent");
  }).length;
}

function answered(pid: string, answer: Json): void {
  const s = S();
  const n = s.notifications[pid];
  n.status = "answered";
  n.answered_at = nowIso();
  n.answerable = false;
  n.answer = answer;
  s.inbox = s.inbox.filter((x) => x.public_id !== pid);
}

function result(n: Json, outcome: string, message: string, extra: Record<string, unknown> = {}): Json {
  return {
    outcome,
    message,
    public_id: n.public_id,
    kind: n.kind,
    status: n.status,
    trip_id: n.trip_id ?? null,
    follow_up: null,
    notify_user: false,
    ...extra,
  };
}

function actionPurpose(action: Json): string {
  const title = String(action?.title ?? "");
  const m = /^Tjänst:\s*(.+)$/i.exec(title);
  return m ? m[1].trim() : title;
}

/** Ägaren gör platsen till bekräftad kund (Stämmer, företagsnamn). */
function confirmCustomer(locationId: string | null | undefined, company?: string): Json | null {
  const loc = S().locations.find((l) => l.id === locationId);
  if (!loc) return null;
  loc.type = "customer";
  loc.confirmed = true;
  if (company) {
    loc.company = company;
    loc.name = company;
  } else if (!loc.company) {
    loc.company = loc.name;
  }
  syncLocation(loc);
  return loc;
}

function respond(pid: string, body: Json): Json {
  const s = S();
  const n = s.notifications[pid];
  if (!n) throw new HttpError(404, "Frågan hittades inte.", "not_found");
  if (n.status === "answered" || n.status === "cancelled" || n.status === "expired") {
    return result(n, "expired", "Frågan är redan besvarad eller inte längre aktuell.", { notify_user: true });
  }
  const key = String(body.action ?? "");
  const action = (n.actions ?? []).find((a: Json) => a.key === key);
  if (!action) throw new HttpError(400, "Okänt svar på frågan.", "invalid_action");
  const reply = typeof body.reply_text === "string" ? body.reply_text.trim().slice(0, 200) : "";
  const tripId: string | null = n.trip_id ?? null;
  const always = !!body.always || key.includes("ALWAYS");

  if (key === "LATER") {
    n.status = "pending";
    n.send_after = new Date(Date.now() + 120 * 60_000).toISOString();
    s.inbox = s.inbox.filter((x) => x.public_id !== pid);
    deliverNext();
    persist();
    return result(n, "snoozed", "Frågan kommer tillbaka om 2 timmar.");
  }
  if (!tripId || !s.trips[tripId]) {
    answered(pid, { action: key });
    persist();
    return result(n, "applied", "Tack!");
  }
  const t = s.trips[tripId].trip;
  let message: string;
  const extra: Record<string, unknown> = {};
  if (key === "OK" && n.kind === "assumed") {
    confirmCustomer(t.end_location_id);
    t.approval_source = "rule";
    t.approval_reason = `Din regel för ${t.end_location?.name ?? "platsen"}`;
    message = "Tack. Platsen räknas som kund, och nästa besök godkänns som tjänst.";
    extra.category = "work";
  } else if (key === "COMPANYTEXT") {
    if (!reply) throw new HttpError(400, "Skriv företagets namn.", "reply_required");
    confirmCustomer(t.end_location_id, reply);
    decide(tripId, { category: "work", purpose: `Kundbesök ${reply}` });
    message = `Sparat: Kundbesök ${reply}.`;
    extra.category = "work";
    extra.company = reply;
  } else if (key.startsWith("PRIV")) {
    const wasAssumed = t.approval_source === "assumed";
    decide(tripId, { category: "private", purpose: null });
    if (always) privateRule(t);
    message = wasAssumed
      ? "Sparat som privat. Platsen antas inte som kund igen."
      : always
        ? "Sparat som privat. Resor hit blir privata av sig själva i fortsättningen."
        : "Sparat som privat.";
    extra.category = "private";
  } else if (key.startsWith("WORK") || key.startsWith("PURPOSE")) {
    const purpose = action.text_input ? reply : actionPurpose(action);
    if (action.text_input && !purpose) throw new HttpError(400, "Skriv ett ärende.", "reply_required");
    decide(tripId, { category: "work", purpose });
    message = always ? `Sparat: Tjänst – ${purpose}. Resor hit godkänns så i fortsättningen.` : `Sparat: Tjänst – ${purpose}.`;
    extra.category = "work";
  } else {
    throw notInDemo();
  }
  answered(pid, { action: key, reply_text: reply || null });
  closeFollowUps(tripId, pid);
  persist();
  return result(n, "applied", message, extra);
}

/** "Alltid privat här": platsen märks som privat (resor dit blir privata utan fråga). */
function privateRule(t: Json): void {
  const loc = S().locations.find((l) => l.id === t.end_location_id);
  if (loc && loc.type !== "home") {
    loc.type = "private";
    loc.confirmed = true;
    syncLocation(loc);
  }
}

/** Månadens genomgång: Stämmer/Fel på en rad som automatiken avgjorde. */
function reviewRow(tripId: string, verdict: string): Json {
  const t = trip(tripId);
  if (!AUTO_SOURCES.has(t.approval_source)) throw new HttpError(400, "Raden är redan avgjord av dig.", "not_automatic");
  let message: string;
  if (verdict === "ok") {
    if (t.approval_source === "assumed") {
      confirmCustomer(t.end_location_id);
      message = "Platsen räknas som kund.";
    } else {
      message = "Sparat som ditt beslut.";
    }
    t.approval_source = "user";
    t.approval_reason = null;
    t.category_source = "user";
    if (t.purpose) t.purpose_confirmed = true;
    closeFollowUps(tripId);
  } else if (t.category === "work") {
    decide(tripId, { category: "private", purpose: null });
    message = "Raden är nu privat.";
  } else {
    // En privat rad öppnas igen och får en fråga.
    t.review_status = "needs_review";
    t.category = "undecided";
    t.approval_source = null;
    t.approval_reason = null;
    splitKm(t);
    openQuestion(tripId);
    message = "Raden är öppnad igen – du får en fråga om den.";
  }
  persist();
  return { message, trip_id: tripId, trip: clone(t) };
}

/** Ny fråga om en resa (id ur demots förbyggda uppsättning, så att /m/q/<id> finns). */
function openQuestion(tripId: string): void {
  const s = S();
  const t = s.trips[tripId].trip;
  if (s.nextQuestion >= POOL_SIZE) return;
  const pid = poolQuestionId(s.nextQuestion++);
  const now = nowIso();
  const when = `${localDay(t.sort_time).slice(8, 10).replace(/^0/, "")}/${Number(localDay(t.sort_time).slice(5, 7))}`;
  const route = `${t.start_location?.name ?? t.start_address} → ${t.end_location?.name ?? t.end_address}`;
  const message = `${when} · ${route} · ${String((Number(t.distance_km) / 10).toFixed(1)).replace(".", ",")} mil. Tjänst eller privat?`;
  s.notifications[pid] = {
    public_id: pid,
    kind: "classify",
    title: "Ny resa",
    message,
    detail_message: message,
    status: "pending",
    channel: null,
    created_at: now,
    sent_at: null,
    answered_at: null,
    expires_at: new Date(Date.now() + 48 * 3600_000).toISOString(),
    send_after: now,
    reminder_count: 0,
    expired: false,
    answerable: true,
    in_digest: false,
    actions: [
      { key: "WORKTEXT", title: "Tjänst – skriv ärende", text_input: true, uri: null },
      { key: "PRIVATE", title: "Privat", text_input: false, uri: null },
      { key: "LATER", title: "Senare", text_input: false, uri: null },
    ],
    trip_id: tripId,
    review_item_id: null,
    last_error: null,
    trip: tripSummary(tripId),
    merge_trips: [],
    summary: null,
    answer: null,
  };
  s.notificationOrder.unshift(pid);
  s.review.unshift({
    id: crypto.randomUUID(),
    vehicle_id: s.vehicleId,
    kind: "category",
    title: `Arbete eller privat? ${t.start_address} → ${t.end_address}`,
    detail: null,
    data: { trip_id: tripId },
    status: "open",
    logical_trip_id: tripId,
    snoozed_until: null,
    created_at: now,
    resolved_at: null,
    resolution: {},
    kind_label: s.reviewMeta.kind_labels.category,
    trip_id: tripId,
  });
  deliverNext();
}

// ----------------------------------------------------------------------------- platser

function syncLocation(loc: Json): void {
  const brief = (b: Json) =>
    b && b.id === loc.id
      ? { ...b, name: loc.name, type: loc.type, confirmed: loc.confirmed, latitude: loc.latitude, longitude: loc.longitude }
      : b;
  for (const d of Object.values(S().trips)) {
    d.trip.start_location = brief(d.trip.start_location);
    d.trip.end_location = brief(d.trip.end_location);
    for (const stop of d.map?.stops ?? []) stop.location = brief(stop.location);
  }
  recountLocations();
}

function recountLocations(): void {
  const s = S();
  const active = s.locations.filter((l) => !l.merged_into_id);
  const byType: Record<string, number> = {};
  for (const l of active) byType[l.type] = (byType[l.type] ?? 0) + 1;
  for (const meta of [s.locationMeta, s.locationMetaAfterHome]) {
    meta.by_type = byType;
    meta.unconfirmed = active.filter((l) => !l.confirmed).length;
  }
}

const LOCATION_FIELDS = ["name", "type", "address", "company", "latitude", "longitude", "radius_m", "confirmed", "notes"];

function patchLocation(id: string, body: Json): Json {
  const loc = S().locations.find((l) => l.id === id);
  if (!loc) throw notFound("Platsen");
  for (const k of LOCATION_FIELDS) if (k in body && body[k] !== undefined) loc[k] = body[k];
  if (body.clear_coordinates) {
    loc.latitude = null;
    loc.longitude = null;
  }
  if (body.latitude != null) loc.coordinate_source = "user";
  syncLocation(loc);
  persist();
  return clone(loc);
}

function createLocation(body: Json): Json {
  if (!body.name) throw new HttpError(400, "Platsen behöver ett namn.");
  const loc = {
    id: crypto.randomUUID(),
    name: body.name,
    type: body.type ?? "unknown",
    address: body.address ?? null,
    city: null,
    company: body.company ?? null,
    latitude: body.latitude ?? null,
    longitude: body.longitude ?? null,
    radius_m: body.radius_m ?? 150,
    coordinate_source: body.latitude != null ? "user" : null,
    confirmed: body.confirmed ?? true,
    is_demo: false,
    suggested_type: null,
    suggested_name: null,
    suggestion_reason: null,
    notes: body.notes ?? null,
    visit_count: 0,
    last_visited_at: null,
    created_from: "user",
    aliases: body.address ? [body.address] : [],
    stats: { arrivals: 0, work: 0, private: 0, undecided: 0, km: 0, most_common_category: null, top_purposes: [] },
  };
  S().locations.push(loc);
  recountLocations();
  persist();
  return clone(loc);
}

function homeFromVehicle(body: Json): Json {
  const s = S();
  if (s.homeMarked && !body.replace) {
    throw new HttpError(
      409,
      "Bostaden är redan markerad (Hemmet). Välj att ersätta den om bilen nu står vid din nya bostad.",
      "home_exists",
    );
  }
  const out = clone(s.homeFromVehicle);
  const loc = s.locations.find((l) => l.id === out.location?.id) ?? s.locations.find((l) => l.type === "home");
  if (loc) {
    loc.latitude = out.location.latitude;
    loc.longitude = out.location.longitude;
    loc.coordinate_source = out.location.coordinate_source ?? "vehicle";
    loc.confirmed = true;
    if (typeof body.address === "string" && body.address.trim()) loc.address = body.address.trim();
    out.location = { ...out.location, ...clone(loc) };
    syncLocation(loc);
  }
  s.homeMarked = true;
  s.locationMeta = clone(s.locationMetaAfterHome);
  recountLocations();
  for (const m of s.monthClose.months) {
    m.notes = (m.notes ?? []).filter((x: Json) => !String(x.code).startsWith("home_missing"));
    m.blockers = (m.blockers ?? []).filter((x: Json) => !String(x.code).startsWith("home_missing"));
  }
  persist();
  return out;
}

// ----------------------------------------------------------------------------- översikten

function inPeriod(t: Json, start: string, end: string): boolean {
  return t.sort_time >= start && t.sort_time < end;
}

const round1 = (n: number) => Math.round(n * 10) / 10;
const kmMil = (km: number) => ({ km: round1(km), mil: round1(km / 10) });

function summarize(base: Json, start: string, end: string): Json {
  const out = clone(base);
  const rows = allTrips().filter((t) => inPeriod(t, start, end));
  const approved = (t: Json) => t.review_status === "approved" || t.review_status === "imported";
  const km = (f: (t: Json) => boolean) => rows.filter(f).reduce((a, t) => a + (Number(t.distance_km) || 0), 0);
  const work = km((t) => t.category === "work");
  const prelWork = km((t) => t.category === "work" && !approved(t));
  const prelPrivate = km((t) => t.category === "private" && !approved(t));
  if (out.distance) {
    out.distance.total = kmMil(km(() => true));
    out.distance.work = kmMil(work);
    out.distance.private = kmMil(km((t) => t.category === "private"));
    out.distance.undecided = kmMil(km((t) => t.category === "undecided"));
    out.distance.preliminary_work = kmMil(prelWork);
    out.distance.preliminary_private = kmMil(prelPrivate);
  }
  if (out.reimbursement) {
    const rate = Number(S().settings.settings.export?.rate_per_mil ?? out.reimbursement.rate_per_mil ?? 25);
    const paid = work - prelWork;
    out.reimbursement.rate_per_mil = rate;
    out.reimbursement.km = round1(paid);
    out.reimbursement.mil = round1(paid / 10);
    out.reimbursement.amount = Math.round((paid / 10) * rate * 100) / 100;
    out.reimbursement.preliminary_mil = round1(prelWork / 10);
    out.reimbursement.preliminary_amount = Math.round((prelWork / 10) * rate * 100) / 100;
    out.reimbursement.show_amount = S().settings.settings.export?.show_amount ?? out.reimbursement.show_amount;
  }
  if (out.trips) {
    out.trips.total = rows.length;
    out.trips.by_category = {
      work: rows.filter((t) => t.category === "work").length,
      private: rows.filter((t) => t.category === "private").length,
      undecided: rows.filter((t) => t.category === "undecided").length,
      mixed: rows.filter((t) => t.has_mixed_categories).length,
    };
    out.trips.preliminary = rows.filter((t) => t.category !== "undecided" && !approved(t)).length;
    out.trips.needs_review = rows.filter((t) => t.review_status === "needs_review").length;
    out.trips.driving_hours = round1(rows.filter((t) => t.category === "work").reduce((a, t) => a + (Number(t.driving_seconds) || 0), 0) / 3600);
  }
  if (out.review) {
    const list = reviewList(new URLSearchParams("status=open"));
    out.review.total = list.open_total;
    out.review.by_kind = Object.entries(list.open_counts as Record<string, number>)
      .filter(([, n]) => n > 0)
      .map(([kind, count]) => ({ kind, label: S().reviewMeta.kind_labels[kind] ?? kind, count }));
  }
  if (out.journal_scope !== undefined) out.journal_scope = S().settings.settings.general?.journal_scope ?? out.journal_scope;
  return out;
}

function timeseries(base: Json): Json {
  const out = clone(base);
  const monthly = out.bucket === "month";
  const groups = new Map<string, Json[]>();
  for (const t of allTrips()) {
    const key = monthly ? localMonth(t.sort_time) : localDay(t.sort_time);
    const list = groups.get(key) ?? [];
    list.push(t);
    groups.set(key, list);
  }
  for (const p of out.points ?? []) {
    const rows = groups.get(String(p.bucket)) ?? [];
    const km = (f: (t: Json) => boolean) => round1(rows.filter(f).reduce((a, t) => a + (Number(t.distance_km) || 0), 0));
    p.trips = rows.length;
    p.total_km = km(() => true);
    p.work_km = km((t) => t.category === "work");
    p.private_km = km((t) => t.category === "private");
    p.undecided_km = km((t) => t.category === "undecided");
    p.preliminary_km = km((t) => t.category !== "undecided" && t.review_status !== "approved" && t.review_status !== "imported");
  }
  return out;
}

// ----------------------------------------------------------------------------- månadsskiftet

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function monthStatus(m: Json): Json {
  const out = clone(m);
  const rows = allTrips().filter((t) => localMonth(t.sort_time) === m.month);
  const open = rows.filter((t) => OPEN_STATUSES.has(t.review_status));
  const blockers: Json[] = [];
  for (const b of out.blockers ?? []) {
    if (b.code === "open_questions") continue;
    if (b.code === "assumption_veto_open") {
      const still = (b.trip_ids ?? []).filter((id: string) => S().trips[id]?.trip.approval_source === "assumed");
      if (!still.length) continue;
      blockers.push({ ...b, trip_ids: still, count: still.length });
      continue;
    }
    blockers.push(b);
  }
  if (open.length && out.state !== "exported") {
    const at = blockers.findIndex((b) => b.code === "month_not_over") + 1;
    blockers.splice(at, 0, {
      code: "open_questions",
      text: `${plural(open.length, "resa väntar", "resor väntar")} på ditt svar`,
      count: open.length,
      trip_ids: open.map((t) => t.id),
      web_only: false,
    });
  }
  out.blockers = blockers;
  out.review_rows = (out.review_rows ?? []).filter((r: Json) => AUTO_SOURCES.has(S().trips[r.trip_id]?.trip.approval_source));
  for (const r of out.review_rows) {
    const t = S().trips[r.trip_id].trip;
    r.category = t.category;
    r.purpose = t.purpose;
    r.approval_source = t.approval_source;
    r.reason = t.approval_reason ?? r.reason;
  }
  return out;
}

function monthClose(q: URLSearchParams): Json {
  const s = S();
  const months = s.monthClose.months.map(monthStatus);
  const want = q.get("month");
  return { ...clone(s.monthClose), months: want ? months.filter((m: Json) => m.month === want) : months };
}

function runMonth(month: string): Json {
  const m = S().monthClose.months.find((x: Json) => x.month === month);
  if (!m) throw new HttpError(400, "Månaden går inte att kontrollera i demot.", "demo_unavailable");
  return monthStatus(m);
}

// ----------------------------------------------------------------------------- export

function createExport(body: Json): Json {
  const s = S();
  const template = s.exports[0];
  const ids: string[] | null = Array.isArray(body.trip_ids) ? body.trip_ids : null;
  const from: string = body.from ?? "";
  const to: string = body.to ?? "";
  const rows = allTrips().filter((t) => {
    if (ids) return ids.includes(t.id);
    const day = localDay(t.sort_time);
    return day >= from && day <= to;
  });
  const work = rows.filter((t) => t.category === "work" && (t.review_status === "approved" || t.review_status === "imported"));
  const km = work.reduce((a, t) => a + (Number(t.distance_km) || 0), 0);
  const rate = Number(s.settings.settings.export?.rate_per_mil ?? 25);
  const label = ids ? "urval" : from.slice(0, 7) === to.slice(0, 7) ? from.slice(0, 7) : `${from}_${to}`;
  const now = nowIso();
  const job = clone(template ?? {});
  Object.assign(job, {
    id: crypto.randomUUID(),
    created_at: now,
    finished_at: now,
    status: "done",
    origin: "manual",
    superseded_by_id: null,
    link_downloads: 0,
    filename: `Körjournal_DEM042_${label}.xlsx`,
    params: { start: from || null, end: to || null, trip_ids: ids, label, layout: s.settings.settings.export?.layout ?? "simple" },
  });
  job.summary = {
    ...(job.summary ?? {}),
    trips: work.length,
    km: round1(km),
    mil: round1(km / 10),
    rate_per_mil: rate,
    amount: Math.round((km / 10) * rate * 100) / 100,
    label,
    period_label: ids ? `${work.length} valda resor` : `${from} – ${to}`,
  };
  s.exports.unshift(job);
  persist();
  return clone(job);
}

// ----------------------------------------------------------------------------- exportens förhandsvisning

const REPORTED = new Set(["approved", "imported"]);
const UNKNOWN_TIME = new Set(["bounded", "unknown"]);
const MONTHS_SV = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"];

/** Klockslagen som underlaget skriver dem (som simple.time_text i backenden). */
function timeText(t: Json): string {
  const clock = (iso: string, quality: string) => `${quality === "estimated" ? "ca " : ""}${localTime(iso)}`;
  const s = t.started_at && !UNKNOWN_TIME.has(t.start_time_quality) ? clock(t.started_at, t.start_time_quality) : "";
  const e = t.ended_at && !UNKNOWN_TIME.has(t.end_time_quality) ? clock(t.ended_at, t.end_time_quality) : "";
  if (s && e) {
    const days = Math.round((Date.parse(`${localDay(t.ended_at)}T00:00:00Z`) - Date.parse(`${localDay(t.started_at)}T00:00:00Z`)) / 86400_000);
    return days > 0 ? `${s}–${e} (+${days})` : `${s}–${e}`;
  }
  if (e) return `–${e}`;
  if (s) return `${s}–`;
  return "";
}

function periodLabel(from: string, to: string): string {
  const last = new Date(Date.UTC(Number(from.slice(0, 4)), Number(from.slice(5, 7)), 0)).getUTCDate();
  if (from.slice(0, 7) === to.slice(0, 7) && from.slice(8) === "01" && Number(to.slice(8)) === last) {
    const name = MONTHS_SV[Number(from.slice(5, 7)) - 1];
    return `${name[0].toUpperCase()}${name.slice(1)} ${from.slice(0, 4)}`;
  }
  return `${from} – ${to}`;
}

/** Inspelad rad (samma text som backenden skrev) eller en framräknad. */
function recordedRows(): Map<string, Json> {
  const map = new Map<string, Json>();
  for (const [k, v] of Object.entries(seed!.statics)) {
    if (k.startsWith("/exports/preview")) for (const r of v.json.rows ?? []) map.set(r.trip_id, r);
  }
  return map;
}

function periodTrips(q: URLSearchParams): { from: string; to: string; rows: Json[] } {
  const from = q.get("from") ?? "";
  const to = q.get("to") ?? "";
  const ids = q.getAll("trip_ids");
  const rows = allTrips()
    .filter((t) => (ids.length ? ids.includes(t.id) : localDay(t.sort_time) >= from && localDay(t.sort_time) <= to))
    .sort((a, b) => (a.sort_time < b.sort_time ? -1 : 1));
  if (ids.length || !from || !to) {
    // Ett urval: perioden är urvalets första och sista dag.
    const days = rows.map((t) => localDay(t.sort_time));
    const today = localDay(new Date());
    return { from: days[0] ?? today, to: days[days.length - 1] ?? today, rows };
  }
  return { from, to, rows };
}

function recordedFor(prefix: string, q: URLSearchParams): Json {
  const key = Object.keys(seed!.statics).find(
    (k) => k.startsWith(prefix) && k.includes(`from=${q.get("from")}&`) && k.includes(`to=${q.get("to")}&`),
  );
  const any = Object.keys(seed!.statics).find((k) => k.startsWith(prefix));
  return { exact: !!key, json: clone(seed!.statics[key ?? any!].json) };
}

function periodBounds(from: string, to: string): Json {
  const start = new Date(`${from}T00:00:00`);
  const end = new Date(new Date(`${to}T00:00:00`).getTime() + 86400_000);
  return { start: start.toISOString().replace(".000Z", "Z"), end: end.toISOString().replace(".000Z", "Z"), timezone: "Europe/Stockholm" };
}

function exportPreview(q: URLSearchParams): Json {
  const { from, to, rows } = periodTrips(q);
  const { json: out } = recordedFor("/exports/preview", q);
  const rec = recordedRows();
  const work = rows.filter((t) => t.category === "work");
  const included = work.filter((t) => REPORTED.has(t.review_status));
  const pending = work.filter((t) => !REPORTED.has(t.review_status));
  const undecided = rows.filter((t) => t.category === "undecided");
  const sumKm = (list: Json[]) => round1(list.reduce((a, t) => a + (Number(t.distance_km) || 0), 0));
  const label = periodLabel(from, to);
  const rate = Number(S().settings.settings.export?.rate_per_mil ?? 25);
  const reg = String(S().vehicles[0]?.registration_number ?? "").replace(/\s+/g, "");
  out.period = { ...periodBounds(from, to), label };
  out.period_label = label;
  out.filename = `Körjournal_${reg}_${from.slice(0, 7) === to.slice(0, 7) ? from.slice(0, 7) : `${from}_${to}`}.xlsx`;
  if (out.header) {
    out.header.period = label;
    out.header.driver = S().settings.settings.general?.driver_name || out.header.driver;
    out.header.employer = S().settings.settings.general?.employer_name || out.header.employer;
    out.header.vehicle = S().vehicles[0]?.name ?? out.header.vehicle;
    out.header.registration_number = S().vehicles[0]?.registration_number ?? out.header.registration_number;
  }
  out.rows = included.map((t) => {
    const r = rec.get(t.id);
    const overridden = !!S().trips[t.id]?.demo_original_start;
    return {
      trip_id: t.id,
      date: localDay(t.sort_time),
      time: r && !overridden ? r.time : timeText(t),
      from: r?.from ?? t.start_address,
      to: r?.to ?? t.end_address,
      purpose: t.purpose ?? "",
      odometer_start: t.odometer_start,
      odometer_end: t.odometer_end,
      km: Math.round(Number(t.distance_km) || 0),
    };
  });
  const km = sumKm(included);
  out.totals = { ...(out.totals ?? {}), trips: included.length, km, mil: round1(km / 10), rate_per_mil: rate, amount: Math.round((km / 10) * rate * 100) / 100 };
  out.excluded = {
    pending_approval: { count: pending.length, km: sumKm(pending), trip_ids: pending.map((t) => t.id) },
    undecided: { count: undecided.length, km: sumKm(undecided), trip_ids: undecided.map((t) => t.id) },
  };
  return out;
}

function exportChecks(q: URLSearchParams): Json {
  const { from, to, rows } = periodTrips(q);
  const { exact, json: out } = recordedFor("/exports/checks", q);
  const work = rows.filter((t) => t.category === "work");
  const reported = work.filter((t) => REPORTED.has(t.review_status));
  const undecided = rows.filter((t) => t.category === "undecided");
  const preliminary = work.filter((t) => !REPORTED.has(t.review_status));
  const sumKm = (list: Json[]) => round1(list.reduce((a, t) => a + (Number(t.distance_km) || 0), 0));
  const check = (code: string, severity: string, title: string, message: string, list: Json[] | null, data: Json = {}) => ({
    code,
    severity,
    title,
    message,
    count: list ? list.length : 1,
    trip_ids: list ? list.map((t) => t.id) : [],
    data,
  });
  const computed = new Set(["no_trips", "undecided_category", "preliminary_classification", "missing_start_time", "estimated_times", "missing_purpose"]);
  const checks: Json[] = [];
  if (!reported.length) {
    checks.push(check("no_trips", "warning", "Inga tjänsteresor att redovisa", rows.length ? "Perioden innehåller inga godkända tjänsteresor." : "Perioden innehåller inga resor.", null));
  }
  if (undecided.length) {
    const km = sumKm(undecided);
    checks.push(check("undecided_category", "warning", "Resor utan avgjord kategori", `${undecided.length} resor (${km.toFixed(1)} km) är varken klassade som arbete eller privat. De tas inte med i underlaget förrän kategorin har avgjorts.`, undecided, { km }));
  }
  if (preliminary.length) {
    const km = sumKm(preliminary);
    checks.push(check("preliminary_classification", "warning", "Preliminära klassningar är inte godkända", `${preliminary.length} tjänsteresor (${km.toFixed(1)} km) har en preliminär klassning som inte godkänts av föraren. De tas inte med i underlaget förrän de har godkänts.`, preliminary, { km }));
  }
  const noPurpose = reported.filter((t) => !t.purpose);
  if (noPurpose.length) checks.push(check("missing_purpose", "error", "Syfte saknas för tjänsteresor", `${noPurpose.length} godkända tjänsteresor saknar ärende/syfte.`, noPurpose));
  // Kontroller som inte räknas om i demot (från inspelningen av samma period).
  if (exact) for (const c of out.checks ?? []) if (!computed.has(c.code)) checks.push(c);
  const noStart = reported.filter((t) => !t.started_at || UNKNOWN_TIME.has(t.start_time_quality));
  if (noStart.length) checks.push(check("missing_start_time", "warning", "Starttid saknas", `${noStart.length} tjänsteresor saknar starttid. Underlaget visar då bara sluttiden (till exempel –08:52). Du kan ange starttiden på resan.`, noStart));
  const estimated = reported.filter((t) => t.start_time_quality === "estimated" || t.end_time_quality === "estimated");
  if (estimated.length) checks.push(check("estimated_times", "info", "Uträknade tider", `${estimated.length} tjänsteresor har en tid som är uträknad ur bilens egna uppgifter. Den står som "ca" i underlaget.`, estimated));
  const count = (sev: string) => checks.filter((c) => c.severity === sev).length;
  const km = sumKm(reported);
  const rate = Number(S().settings.settings.export?.rate_per_mil ?? 25);
  out.period = periodBounds(from, to);
  out.checks = checks;
  out.errors = count("error");
  out.warnings = count("warning");
  out.infos = count("info");
  out.is_complete = out.errors === 0;
  out.status_label = out.errors ? "Behöver åtgärdas" : out.status_label;
  out.summary = {
    ...(out.summary ?? {}),
    trips: reported.length,
    km,
    mil: round1(km / 10),
    rate_per_mil: rate,
    amount: Math.round((km / 10) * rate * 100) / 100,
    period_label: periodLabel(from, to),
    excluded: {
      pending_approval: { count: preliminary.length, km: sumKm(preliminary) },
      undecided: { count: undecided.length, km: sumKm(undecided) },
      total: preliminary.length + undecided.length,
    },
  };
  return out;
}

// ----------------------------------------------------------------------------- inställningar

function patchSettings(section: string, body: Json): Json {
  const s = S();
  const current = s.settings.settings[section];
  if (!current) throw new HttpError(404, "Sektionen hittades inte.", "not_found");
  const before = current.mode;
  Object.assign(current, clone(body));
  if (section === "automation" && body.mode === "self_driving" && before !== "self_driving") current.active_since = nowIso();
  persist();
  return clone(current);
}

// ----------------------------------------------------------------------------- import

function importPreview(): Json {
  const s = S();
  const batch = clone(s.importPreview);
  batch.created_at = nowIso();
  s.imports = [batch, ...s.imports.filter((b) => b.id !== batch.id)];
  persist();
  return batch;
}

// ----------------------------------------------------------------------------- statiska svar

function fromRecording(path: string, query: URLSearchParams): Json | undefined {
  const statics = seed!.statics;
  const qs = query.toString();
  const exact = statics[qs ? `${path}?${qs}` : path];
  if (exact) return exact;
  // Samma sökväg med andra parametrar: välj ett svar med samma period om det finns.
  const candidates = Object.keys(statics).filter((k) => k.split("?")[0] === path);
  if (!candidates.length) return undefined;
  const period = query.get("period");
  const best =
    candidates.find((k) => period && new URLSearchParams(k.split("?")[1] ?? "").get("period") === period) ??
    candidates.find((k) => new URLSearchParams(k.split("?")[1] ?? "").get("period") === "month") ??
    candidates[0];
  return statics[best];
}

/** Översiktens svar räknas om ur resorna (period ur inspelningen eller from/to). */
function dashboard(kind: string, path: string, q: URLSearchParams): Json {
  const rec = fromRecording(path, q);
  if (!rec) throw notFound("Statistiken");
  if (kind === "summary") {
    const p = rec.json.period ?? {};
    let start = p.start;
    let end = p.end;
    if (q.get("period") === "custom" && q.get("from") && q.get("to")) {
      start = new Date(`${q.get("from")}T00:00:00`).toISOString();
      end = new Date(new Date(`${q.get("to")}T00:00:00`).getTime() + 86400_000).toISOString();
    }
    return summarize(rec.json, start ?? "0000", end ?? "9999");
  }
  if (kind === "timeseries") return timeseries(rec.json);
  if (kind === "destinations") return destinations(rec.json);
  return rec.json;
}

/** Destinationerna: platsens namn och fördelningen arbete/privat följer ägarens ändringar. */
function destinations(base: Json): Json {
  const out = clone(base);
  const start = out.period?.start ?? "0000";
  const end = out.period?.end ?? "9999";
  const rows = allTrips().filter((t) => inPeriod(t, start, end));
  const home = S().locations.find((l) => l.type === "home");
  for (const item of out.items ?? []) {
    const locId = item.location_id ?? (item.type === "home" ? home?.id : null);
    if (!locId) continue;
    const loc = S().locations.find((l) => l.id === locId);
    if (loc && item.location_id) {
      item.label = loc.name;
      item.type = loc.type;
    }
    const mine = rows.filter((t) => t.end_location_id === locId);
    if (mine.length !== item.trips) continue; // räknas inte likadant som servern – behåll inspelningen
    const km = (c: string) => round1(mine.filter((t) => t.category === c).reduce((a, t) => a + (Number(t.distance_km) || 0), 0));
    item.work_km = km("work");
    item.private_km = km("private");
    item.undecided_km = km("undecided");
  }
  return out;
}

// ----------------------------------------------------------------------------- router

interface Ctx {
  method: string;
  path: string;
  query: URLSearchParams;
  body: Json;
  m: RegExpExecArray;
}
type Handler = (c: Ctx) => unknown;
const routes: [string, RegExp, Handler][] = [];
const on = (method: string, pattern: string, handler: Handler) =>
  routes.push([method, new RegExp(`^${pattern.replace(/:id/g, "([^/]+)")}$`), handler]);

const ok204 = Symbol("204");

// Inloggning: demot har ingen inloggning – ägaren är alltid inloggad som den påhittade användaren.
on("GET", "/auth/setup-status", () => ({ needs_setup: false }));
on("GET", "/auth/me", () => S().me);
on("POST", "/auth/login", () => S().me);
on("POST", "/auth/logout", () => ok204);
on("POST", "/auth/setup", () => S().me);

on("GET", "/vehicles", () => S().vehicles);
on("PATCH", "/vehicles/:id", ({ m, body }) => {
  const v = S().vehicles.find((x) => x.id === m[1]);
  if (!v) throw notFound("Fordonet");
  for (const k of ["name", "make", "model", "model_year", "registration_number"]) if (k in body) v[k] = body[k];
  persist();
  return clone(v);
});
on("GET", "/vehicles/:id/odometer", () => clone(S().odometer ?? []));
on("POST", "/vehicles/:id/odometer", ({ body }) => {
  const at = new Date(body.at);
  if (Number.isNaN(at.getTime())) throw new HttpError(400, "Ange datum och tid.");
  const km = Number(body.odometer_km);
  if (!Number.isFinite(km) || km < 0) throw new HttpError(400, "Ange en mätarställning i km.");
  const snap = { id: crypto.randomUUID(), at: at.toISOString(), odometer_km: km, source: "manual", note: body.note || "Inmatad för hand" };
  const list = (S().odometer ??= []);
  list.push(snap);
  list.sort((a, b) => (a.at < b.at ? 1 : -1));
  persist();
  return clone(snap);
});
on("DELETE", "/vehicles/:id/odometer/:id", ({ m }) => {
  S().odometer = (S().odometer ?? []).filter((x) => x.id !== m[2]);
  persist();
  return ok204;
});

// Bränsle: demobilen har inte tankat under perioden.
on("POST", "/energy/detect-refuels", () => ({ created: 0, items: [] }));

// Ångra (lärande-feedback)
on("GET", "/learning/feedback", ({ query }) => feedbackList(query));
on("POST", "/learning/feedback/:id/undo", ({ m }) => undoFeedback(m[1]));

// Tjänsteställen
on("GET", "/settings/workplaces/list", () => clone(S().workplaces ?? []));
on("POST", "/settings/workplaces", ({ body }) => {
  const loc = S().locations.find((l) => l.id === body.location_id);
  if (body.kind === "location" && !loc) throw new HttpError(400, "Välj en plats.");
  const wp = {
    id: crypto.randomUUID(),
    kind: body.kind,
    location_id: body.kind === "location" ? loc.id : null,
    location_name: body.kind === "location" ? loc.name : null,
    valid_from: body.valid_from ?? null,
    valid_to: body.valid_to ?? null,
    note: body.note ?? "",
  };
  (S().workplaces ??= []).push(wp);
  persist();
  return clone(wp);
});
on("DELETE", "/settings/workplaces/:id", ({ m }) => {
  S().workplaces = (S().workplaces ?? []).filter((w) => w.id !== m[1]);
  persist();
  return ok204;
});

// Resor
on("GET", "/trips", ({ query }) => listTrips(query));
on("GET", "/trips/:id", ({ m }) => tripDetail(m[1]));
on("PATCH", "/trips/:id", ({ m, body }) => {
  const id = m[1];
  const t = trip(id);
  if (body.is_favorite !== undefined && body.is_favorite !== null) t.is_favorite = !!body.is_favorite;
  if ("start_time" in body) setStartTime(id, body.start_time);
  if (["category", "purpose", "notes"].some((k) => k in body) || body.approve) {
    decide(id, {
      category: body.category ?? undefined,
      purpose: "purpose" in body ? body.purpose : undefined,
      notes: "notes" in body ? body.notes : undefined,
      approve: !!body.approve,
    });
  }
  persist();
  return clone(trip(id));
});
on("POST", "/trips/:id/approve", ({ m }) => {
  const t = decide(m[1], { approve: true });
  persist();
  return clone(t);
});
on("POST", "/trips/bulk", ({ body }) => {
  const ids: string[] = body.trip_ids ?? [];
  for (const id of ids) {
    const t = trip(id);
    if (body.is_favorite !== undefined && body.is_favorite !== null) t.is_favorite = !!body.is_favorite;
    if (body.category != null || body.purpose != null || body.approve) {
      decide(id, { category: body.category ?? undefined, purpose: body.purpose ?? undefined, approve: !!body.approve });
    }
  }
  persist();
  return { updated: ids.length, errors: [] };
});

on("POST", "/trips/manual", ({ body }) => createManualTrip(body));
on("DELETE", "/trips/:id", ({ m }) => {
  const t = trip(m[1]);
  if (t.source !== "manual") throw new HttpError(400, "Bara manuellt inlagda resor kan tas bort.", "not_manual");
  t.state = "deleted";
  persist();
  return ok204;
});

// Granskning
on("GET", "/review", ({ query }) => reviewList(query));
on("POST", "/review/:id/(dismiss|resolve|reopen|snooze)", ({ m, body }) => {
  const item = reviewItem(m[1]);
  const action = m[2];
  item.status = action === "reopen" ? "open" : action === "snooze" ? "snoozed" : action === "dismiss" ? "dismissed" : "resolved";
  item.resolved_at = action === "reopen" ? null : nowIso();
  if (action === "snooze") item.snoozed_until = new Date(Date.now() + (Number(body.minutes) || 24 * 60) * 60_000).toISOString();
  persist();
  return reviewItemOut(item);
});
on("GET", "/gaps", ({ path, query }) => fromRecording(path, query)?.json ?? { items: [], total: 0 });

// Frågor
on("GET", "/notifications", () => S().notificationOrder.map((pid) => listItem(S().notifications[pid])));
on("GET", "/notifications/demo-inbox", () => clone(S().inbox));
on("GET", "/notifications/:id", ({ m }) => notificationDetail(m[1]));
on("POST", "/notifications/:id/respond", ({ m, body }) => respond(m[1], body));
on("POST", "/notifications/:id/snooze", ({ m }) => respond(m[1], { action: "LATER" }));
on("POST", "/notifications/review-rows/:id", ({ m, body }) => reviewRow(m[1], body.verdict));

// Platser
on("GET", "/locations", () => clone(S().locations.filter((l) => !l.merged_into_id)));
on("GET", "/locations/meta", () => clone(S().locationMeta));
on("GET", "/locations/suggestions", () => ({
  types: clone(S().locations.filter((l) => !l.merged_into_id && l.suggested_type && !l.confirmed && l.suggested_type !== l.type)),
  merges: [],
}));
on("POST", "/locations", ({ body }) => createLocation(body));
on("POST", "/locations/home-from-vehicle", ({ body }) => homeFromVehicle(body));
on("PATCH", "/locations/:id", ({ m, body }) => patchLocation(m[1], body));
// "Hämta adressen från kartan": demot slår aldrig upp något – platsens egen (påhittade) adress,
// annars en påhittad, fylls i som förslag.
on("POST", "/locations/:id/lookup-address", ({ m }) => {
  const loc = S().locations.find((l) => l.id === m[1]);
  if (!loc) throw notFound("Platsen");
  if (loc.latitude == null || loc.longitude == null) throw new HttpError(400, "Platsen har ingen position. Sätt ut den på kartan och spara först.", "no_position");
  const address = loc.address ?? "Exempelvägen 1, 123 45 Exempelstad";
  return { address, city: address.split(",").pop()?.trim() ?? null };
});
on("POST", "/locations/:id/accept-suggestion", ({ m }) => {
  const loc = S().locations.find((l) => l.id === m[1]);
  if (!loc) throw notFound("Platsen");
  return patchLocation(m[1], {
    type: loc.suggested_type ?? loc.type,
    name: loc.suggested_name ?? loc.name,
    confirmed: true,
  });
});
on("POST", "/locations/:id/reject-suggestion", ({ m }) => {
  const loc = S().locations.find((l) => l.id === m[1]);
  if (!loc) throw notFound("Platsen");
  loc.suggested_type = null;
  loc.suggested_name = null;
  loc.suggestion_reason = null;
  persist();
  return clone(loc);
});

// Inställningar
on("GET", "/settings", () => clone(S().settings));
on("PATCH", "/settings/:id", ({ m, body }) => patchSettings(m[1], body));

// Integrationer
on("GET", "/integrations/home-assistant", () => clone(S().ha));
on("GET", "/integrations/volvo/status", () => clone(S().volvo));
// Kalenderlänken (ICS) hämtar en riktig kalender, så demot visar alltid läget "ingen länk".
const ICS_NONE = {
  provider: "calendar_ics",
  status: "not_configured",
  has_link: false,
  host: null,
  last_sync_at: null,
  last_success_at: null,
  last_error: null,
  last_error_at: null,
  stored_events: 0,
  events_with_location: null,
  tip: null,
  stores_body_preview: false,
  sync_days_back: 60,
  sync_days_forward: 14,
};
const icsNotInDemo = () => new HttpError(501, "Kalenderlänken går inte att prova i demot – den hämtar en riktig kalender.", "demo_unavailable");
on("GET", "/integrations/microsoft/ics", () => clone(ICS_NONE));
on("PUT", "/integrations/microsoft/ics", () => {
  throw icsNotInDemo();
});
on("DELETE", "/integrations/microsoft/ics", () => {
  throw icsNotInDemo();
});
on("POST", "/integrations/microsoft/ics/sync", () => {
  throw icsNotInDemo();
});
// Synka och testa: svarar som en frisk anslutning (inga nya resor sedan sist).
on("POST", "/integrations/volvo/sync", () => {
  const now = nowIso();
  S().volvo.last_sync_at = now;
  persist();
  return {
    vehicle_id: S().vehicleId,
    result: { status: "ok", new_trips: 0, message: "Inga nya resor sedan sist." },
    last_observation_at: S().volvo.last_observation_at ?? null,
    at: now,
  };
});
on("POST", "/integrations/home-assistant/test", () => {
  const ha = S().ha;
  ha.last_success_at = nowIso();
  persist();
  return {
    ok: true,
    status: "connected",
    message: `Anslutningen fungerar. Home Assistant ${ha.ha_version ?? ""} svarar.`.replace("  ", " "),
    devices: [ha.notify_service].filter(Boolean),
    ha_version: ha.ha_version ?? null,
    location_name: ha.location_name ?? null,
  };
});
on("POST", "/integrations/home-assistant/test-notification", () => {
  const s = S();
  const now = nowIso();
  const pid = `test${Date.now().toString(16)}`;
  s.inbox.unshift({
    public_id: pid,
    kind: "test",
    title: "DriveLog",
    message: "Testnotis: notiserna fungerar.",
    sent_at: now,
    actions: [],
    ha_payload: { title: "DriveLog", message: "Testnotis: notiserna fungerar.", data: { tag: `drivelog-${pid}`, group: "drivelog" } },
  });
  s.ha.last_test_sent_at = now;
  s.ha.last_test_confirmed_at = now;
  persist();
  return { public_id: pid, channel: s.ha.channel ?? "notify", delivered: true, queued: false, demo: false, message: "Testnotisen är skickad till telefonen." };
});

// Export och månadsskiftet
on("GET", "/exports", () => clone(S().exports));
on("POST", "/exports", ({ body }) => createExport(body));
on("DELETE", "/exports/:id", ({ m }) => {
  S().exports = S().exports.filter((e) => e.id !== m[1]);
  persist();
  return ok204;
});
on("GET", "/exports/preview", ({ query }) => exportPreview(query));
on("GET", "/exports/checks", ({ query }) => exportChecks(query));
on("GET", "/month-close", ({ query }) => monthClose(query));
on("POST", "/month-close/links/revoke", () => {
  S().linksRevokedAt = nowIso();
  persist();
  return null;
});
on("POST", "/month-close/:id/run", ({ m }) => runMonth(decodeURIComponent(m[1])));

// Översikten
on("GET", "/dashboard/(summary|timeseries|destinations|routes|monthly)", ({ m, path, query }) => dashboard(m[1], path, query));

// Import (förhandsgranskningen är ett inspelat exempel)
on("GET", "/imports", () => ({ items: clone(S().imports), total: S().imports.length }));
on("GET", "/imports/:id", ({ m }) => {
  if (m[1] !== S().importBatch.id) throw notFound("Importen");
  return clone(S().importBatch);
});
on("GET", "/imports/:id/rows", ({ query }) => {
  const statuses = query.getAll("status");
  const key = ["page=1&page_size=50", ...statuses.map((s) => `status=${s}`)].join("&");
  return clone(S().importRows[key] ?? S().importRows["page=1&page_size=50"]);
});
on("POST", "/imports/preview", () => importPreview());
on("POST", "/imports/:id/undo", ({ m }) => {
  S().imports = S().imports.filter((b) => b.id !== m[1]);
  persist();
  return { undone: true, status: "discarded" };
});

// ----------------------------------------------------------------------------- ingång

async function dispatch(method: string, url: URL, body: Json): Promise<Response> {
  const path = url.pathname.slice(API.length).replace(/\/$/, "") || "/";
  for (const [m, re, handler] of routes) {
    if (m !== method) continue;
    const match = re.exec(path);
    if (!match) continue;
    const out = handler({ method, path, query: url.searchParams, body, m: match });
    if (out === ok204) return json(undefined, 204);
    return json(out ?? null, out === null ? 200 : method === "POST" && /^\/(exports|locations|trips\/manual)$/.test(path) ? 201 : 200);
  }
  if (method === "GET") {
    const rec = fromRecording(path, url.searchParams);
    if (rec) return json(clone(rec.json), rec.status);
    (window.__drivelogDemoUnhandled ??= []).push(`${method} ${path}`);
    throw new HttpError(404, NOT_IN_DEMO, "demo_unavailable");
  }
  throw notInDemo();
}

function readBody(init?: RequestInit): Json {
  if (!init?.body || typeof init.body !== "string") return {};
  try {
    return JSON.parse(init.body);
  } catch {
    return {};
  }
}

/** Besvarar ett fetch-anrop mot /api/v1 lokalt (ersätter nätverket i demot). */
export async function handleFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  await boot();
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, window.location.href);
  const method = (init?.method ?? "GET").toUpperCase();
  await new Promise((r) => setTimeout(r, LATENCY_MS));
  try {
    return await dispatch(method, url, readBody(init));
  } catch (err) {
    if (err instanceof HttpError) return json({ detail: err.detail, code: err.code, data: err.data }, err.status);
    console.warn("Demots server:", err);
    return json({ detail: "Något gick fel i demot. Prova Återställ demot.", code: "demo_error" }, 500);
  }
}

/** Fångar fetch-anrop mot /api som inte går via api.ts (skyddsnät: inget når nätverket). */
export function installFetchGuard(): void {
  const original = window.fetch.bind(window);
  if ((original as { __demo?: boolean }).__demo) return;
  const guarded = (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, window.location.href);
    if (url.origin === window.location.origin && url.pathname.startsWith("/api/")) return handleFetch(url.href, init);
    return original(input, init);
  };
  (guarded as { __demo?: boolean }).__demo = true;
  window.fetch = guarded as typeof window.fetch;
}
