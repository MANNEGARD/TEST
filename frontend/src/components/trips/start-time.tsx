"use client";

import * as React from "react";
import { Clock, Pencil, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { FieldError, Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatNumber, formatTime, localDateISO, zonedToISO } from "@/lib/format";
import { useUpdateTrip } from "@/lib/queries/trips";
import type { Trip } from "@/lib/types";
import { TripTime } from "./trip-time";

/** RawTrip.start_estimate_basis (services/trips/start_time.py, byggkontraktet D.3). */
export interface StartBasis {
  method?: "trip_meter" | "odometer_speed" | "window" | "distance_speed" | string;
  earliest?: string | null;
  latest?: string | null;
  margin_min?: number | null;
  trip_km?: number | null;
  avg_speed_kmh?: number | null;
  speed_kmh?: number | null;
  engine_running_at?: string | null;
  window_minutes?: number | null;
  /** K4: varifrån snittfarten kommer, antal egna resor och den uträknade körtiden. */
  speed_source?: "route" | "similar" | "table" | string | null;
  samples?: number | null;
  duration_minutes?: number | null;
  /** K4: körtiden rymdes inte mellan bilens avläsningar, så tiden hölls inom fönstret. */
  held_to_window?: boolean;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export function readBasis(raw: unknown): StartBasis {
  if (!raw || typeof raw !== "object") return {};
  const b = raw as Record<string, unknown>;
  return {
    method: str(b.method) ?? undefined,
    earliest: str(b.earliest),
    latest: str(b.latest),
    margin_min: num(b.margin_min),
    trip_km: num(b.trip_km),
    avg_speed_kmh: num(b.avg_speed_kmh),
    speed_kmh: num(b.speed_kmh),
    engine_running_at: str(b.engine_running_at),
    window_minutes: num(b.window_minutes),
    speed_source: str(b.speed_source),
    samples: num(b.samples),
    duration_minutes: num(b.duration_minutes),
    held_to_window: b.held_to_window === true,
  };
}

/** "±4 min" ur grunden, eller tom text. */
export function marginText(basis: StartBasis): string {
  const m = basis.margin_min;
  return m == null ? "" : `±${Math.max(1, Math.round(m))} min`;
}

/** Grunden för en uträknad starttid i klartext, t.ex. "Bilen stod still 07:30 och var på väg 07:40." */
export function basisText(basis: StartBasis): string {
  const parts: string[] = [];
  if (basis.method === "trip_meter" && basis.trip_km != null && basis.avg_speed_kmh != null) {
    parts.push(
      `Uträknad ur bilens trippmätare: ${formatNumber(basis.trip_km, 1)} km i snitt ${formatNumber(basis.avg_speed_kmh, 0)} km/h fram till ankomsten.`,
    );
  } else if (basis.method === "odometer_speed" && basis.speed_kmh != null) {
    parts.push(`Uträknad ur mätarställningen under färden, som ökade med ungefär ${formatNumber(basis.speed_kmh, 0)} km/h.`);
  } else if (basis.method === "window") {
    parts.push("Tiden är mitt i det korta fönstret mellan bilens avläsningar.");
  } else if (basis.method === "distance_speed" && basis.speed_kmh != null && basis.trip_km != null) {
    const from =
      basis.speed_source === "route"
        ? `snittfarten på dina ${formatNumber(basis.samples ?? 0, 0)} tidigare resor mellan samma platser`
        : basis.speed_source === "similar"
          ? `snittfarten på dina ${formatNumber(basis.samples ?? 0, 0)} tidigare resor med liknande sträcka`
          : "en vanlig snittfart för en så lång resa";
    parts.push(
      `Uppskattad ur sträckan: ${formatNumber(basis.trip_km, 1)} km i ${formatNumber(basis.speed_kmh, 0)} km/h (${from}) ger ungefär ${formatNumber(basis.duration_minutes ?? 0, 0)} minuters körtid före ankomsten. Bilen rapporterade bara när resan var slut.`,
    );
  }
  if (basis.method === "distance_speed") {
    if (basis.earliest && basis.latest) parts.push(`Starten låg någon gång mellan ${formatDateTime(basis.earliest)} och ${formatTime(basis.latest)}.`);
    if (basis.held_to_window) parts.push("Körtiden ryms inte där, så tiden hölls inom det möjliga.");
  } else if (basis.earliest && basis.latest) parts.push(`Bilen stod still ${formatTime(basis.earliest)} och var på väg ${formatTime(basis.latest)}.`);
  else if (basis.latest) parts.push(`Bilen var på väg ${formatTime(basis.latest)}.`);
  if (basis.engine_running_at) parts.push(`Motorn gick ${formatTime(basis.engine_running_at)}.`);
  return parts.join(" ") || "Uträknad ur bilens egna uppgifter.";
}

/** Lokalt klockslag före sluttiden → ISO. Ligger klockslaget efter sluttiden avses dagen före. */
export function startTimeISO(time: string, endedAt: string): string {
  const end = new Date(endedAt).getTime();
  let iso = zonedToISO(localDateISO(endedAt), time);
  if (new Date(iso).getTime() > end) iso = zonedToISO(localDateISO(new Date(end - DAY_MS)), time);
  return iso;
}

/**
 * Starttiden på resans sida (byggkontraktet D.3):
 *  - uträknad (K1–K3) eller uppskattad ur sträckan (K4): "≈ 07:35 (±4 min)", ett tryck visar grunden,
 *  - okänd: "Start okänd (mellan 06:10 och 08:52)" och knappen "Ange starttid",
 *  - angiven: "07:35 · angiven av dig" med "Ändra" och "Ta bort",
 *  - från bilen eller importen: klockslaget som vanligt.
 */
export function StartTime({ trip, earliest, latest }: { trip: Trip; earliest?: string | null; latest?: string | null }) {
  const update = useUpdateTrip();
  const [showBasis, setShowBasis] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const basis = readBasis(trip.start_estimate_basis);
  const lo = earliest ?? trip.start_earliest ?? basis.earliest ?? null;
  const hi = latest ?? trip.start_latest ?? basis.latest ?? null;
  const source = trip.start_time_source;
  const canEdit = !!trip.ended_at;

  const open = () => {
    setDraft(trip.started_at && source !== "unknown" ? formatTime(trip.started_at) : hi ? formatTime(hi) : "");
    setError(null);
    setEditing(true);
  };

  const submit = (value: string | null) => {
    if (!trip.ended_at) return;
    let iso: string | null = null;
    if (value !== null) {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) {
        setError("Ange starttiden som klockslag (TT:MM).");
        return;
      }
      iso = startTimeISO(value, trip.ended_at);
    }
    setError(null);
    update.mutate(
      { id: trip.id, patch: { start_time: iso } },
      {
        onSuccess: () => {
          toast.success(iso ? "Starttiden är sparad." : "Den angivna starttiden är borttagen.");
          setEditing(false);
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  if (editing) {
    return (
      <form
        className="space-y-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          submit(draft);
        }}
        noValidate
      >
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="time"
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Starttid"
            aria-invalid={error ? true : undefined}
            className="w-[7.5rem] num"
            data-testid="start-time-input"
          />
          <Button type="submit" size="sm" variant="primary" loading={update.isPending}>
            Spara
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={update.isPending}>
            Avbryt
          </Button>
        </div>
        {lo || hi ? (
          <p className="text-xs text-fg-muted">
            Möjligt intervall enligt bilen: {lo ? formatDateTime(lo) : "okänt"} – {hi ? formatTime(hi) : "okänt"}.
          </p>
        ) : null}
        <FieldError>{error}</FieldError>
      </form>
    );
  }

  if (source === "unknown") {
    return (
      <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="start-time-unknown">
        <span className="text-fg-muted italic">
          Start okänd
          {lo || hi ? (
            <span className="num not-italic">
              {" "}
              (mellan {lo ? formatTime(lo) : "?"} och {hi ? formatTime(hi) : "?"})
            </span>
          ) : null}
        </span>
        {canEdit ? (
          <Button size="xs" variant="secondary" onClick={open}>
            <Clock aria-hidden /> Ange starttid
          </Button>
        ) : null}
      </span>
    );
  }

  if (source === "user") {
    // Manuellt inlagda resor har sin starttid från inmatningen; den ändras i dialogen för manuella resor.
    return (
      <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="start-time-user">
        <span className="num">{trip.started_at ? formatTime(trip.started_at) : "–"}</span>
        <span className="text-xs text-fg-muted">angiven av dig</span>
        {canEdit && trip.source !== "manual" ? (
          <>
            <Button size="xs" variant="ghost" onClick={open} aria-label="Ändra starttiden">
              <Pencil aria-hidden /> Ändra
            </Button>
            <Button size="xs" variant="ghost" onClick={() => submit(null)} loading={update.isPending} aria-label="Ta bort den angivna starttiden">
              <X aria-hidden /> Ta bort
            </Button>
          </>
        ) : null}
        <FieldError>{error}</FieldError>
      </span>
    );
  }

  if (source === "estimated" && trip.started_at) {
    const margin = marginText(basis);
    return (
      <span className="inline-flex flex-col gap-1" data-testid="start-time-estimated">
        <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
          <button
            type="button"
            aria-expanded={showBasis}
            onClick={() => setShowBasis((v) => !v)}
            className="value-estimated cursor-pointer rounded-sm whitespace-nowrap focus-visible:outline-2 focus-visible:outline-ring"
            title="Visa grunden för tiden"
          >
            <span aria-hidden>≈ </span>
            <span className="sr-only">ungefär </span>
            {formatTime(trip.started_at)}
            {margin ? <span className="text-fg-muted"> ({margin})</span> : null}
          </button>
          {canEdit ? (
            <Button size="xs" variant="ghost" onClick={open} aria-label="Ange starttiden själv">
              <Pencil aria-hidden /> Ange
            </Button>
          ) : null}
        </span>
        {showBasis ? (
          <span className="max-w-xs text-xs leading-relaxed text-fg-muted" data-testid="start-time-basis">
            {basisText(basis)}
          </span>
        ) : null}
      </span>
    );
  }

  return <TripTime at={trip.started_at} quality={trip.start_time_quality} earliest={lo} latest={hi} />;
}
