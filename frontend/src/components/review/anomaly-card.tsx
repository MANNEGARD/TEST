"use client";

import * as React from "react";
import Link from "next/link";
import { AlertTriangle, Check, ExternalLink, Info } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatDate, formatDuration, formatKm, formatOdometer, formatTime } from "@/lib/format";
import type { ReviewItem, Trip } from "@/lib/types";
import { cn } from "@/lib/utils";
import { TripSummary } from "./trip-summary";

/** Avvikelser som gäller fordonet och inte en resa: visas som förut, med texten från servern. */
export const VEHICLE_ANOMALY_TYPES = new Set(["stale_vehicle_data", "stale_data", "odometer_jump", "odometer_decreased", "missed_trips", "large_gap"]);

/** Vart ägaren går för en fordonsavvikelse (när texten redan pekar dit). stale_data skapas av
 *  insamlingen för samma läge som stale_vehicle_data. */
const VEHICLE_LINKS: Record<string, { href: string; label: string }> = {
  stale_vehicle_data: { href: "/integrations", label: "Öppna Integrationer" },
  stale_data: { href: "/integrations", label: "Öppna Integrationer" },
  missed_trips: { href: "/import", label: "Öppna Import" },
};

/** Typen som kortet förklarar: odometer_overlap (omräkningens namn före 1.3.2) är samma fall som
 *  odometer_backwards mellan två resor. */
function anomalyType(item: ReviewItem): string {
  const type = item.data.type ?? "";
  return type === "odometer_overlap" ? "odometer_backwards" : type;
}

/** Resorna som avvikelsen gäller: en (trip) eller två (first_trip, second_trip). */
export function anomalyTrips(item: ReviewItem): Trip[] {
  if (item.first_trip || item.second_trip) return [item.first_trip, item.second_trip].filter((t): t is Trip => !!t);
  return item.trip ? [item.trip] : [];
}

/** Gäller avvikelsen en eller två resor (även om resan inte finns i svaret)? */
function isTripBound(item: ReviewItem): boolean {
  if (anomalyTrips(item).length) return true;
  const d = item.data;
  if (d.type && VEHICLE_ANOMALY_TYPES.has(d.type)) return false;
  return !!(item.logical_trip_id || d.raw_trip_id || d.first_raw_trip_id || d.second_raw_trip_id || d.raw_trip_ids);
}

/** Gäller avvikelsen två resor? */
function isTwoTrips(item: ReviewItem): boolean {
  const d = item.data;
  return !!(
    item.first_trip ||
    item.second_trip ||
    d.first_raw_trip_id ||
    d.second_raw_trip_id ||
    d.first_trip_id ||
    Array.isArray(d.raw_trip_ids) ||
    d.type === "possible_duplicate" ||
    d.type === "overlap" ||
    d.type === "odometer_overlap"
  );
}

/** Datum och klockslag ur ärendets data när resan inte finns i svaret – så att två sådana
 *  ärenden går att skilja åt. */
export function anomalyDate(item: ReviewItem): string | null {
  const d = item.data;
  const iso = [d.started_at, d.first_started_at].find((v): v is string => typeof v === "string" && !!v);
  return iso ? `${formatDate(iso)} ${formatTime(iso)}` : null;
}

function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function secondsBetween(from: string | null | undefined, to: string | null | undefined): number | undefined {
  if (!from || !to) return undefined;
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isFinite(ms) ? ms / 1000 : undefined;
}

interface Explanation {
  /** Vilka värden som inte går ihop. */
  problem: string;
  /** Vilket värde underlaget till arbetsgivaren använder. */
  basis: string;
  /** Vad ägaren gör. */
  action: string;
  /** Vad som händer om ägaren inte gör något. */
  ifNothing: string;
}

/** Vad ägaren kan göra åt sträckan, efter var resan kommer ifrån: en importerad resa ger samma
 *  värden vid en ny import, och en egen resa rättar man själv. */
function fixDistance(trip: Trip | undefined): string {
  if (trip?.source === "volvo_import") return "Uppgifterna kommer ur Volvos körjournal, och en ny import ger samma värden. Stämmer sträckan: välj Avfärda.";
  if (trip?.source === "manual")
    return "Du har lagt in resan själv: öppna den och rätta sträckan eller mätarställningen, och markera sedan ärendet som hanterat. Stämmer sträckan: välj Avfärda.";
  return "Öppna resan och jämför med kartan. Stämmer sträckan: välj Avfärda. Är den fel: importera Volvos körjournal för dagen (den ersätter bilens uppgifter om resan) och markera sedan ärendet som hanterat.";
}

/** Vad ägaren kan göra åt klockslagen. Tidsavvikelser skapas bara när tiderna kommer från källan,
 *  och sådana starttider går inte att ändra på resan (egna resor rättar man i resan). */
function fixTimes(trip: Trip | undefined): string {
  if (trip?.source === "manual") return "Du har angett tiderna själv: öppna resan och rätta dem, och markera sedan ärendet som hanterat. Stämmer de: välj Avfärda.";
  const origin = trip?.source === "volvo_import" ? "ur Volvos körjournal" : "från bilen";
  return `Klockslagen kommer ${origin} och går inte att ändra i DriveLog. Stämmer resan i övrigt: välj Avfärda.`;
}

const FIX_COPY = "Är den ena en kopia av den andra: öppna den och ta bort den (det går för importerade och egna resor), och markera sedan ärendet som hanterat.";

/** Förklaringen i klartext per avvikelsetyp. Talen tas ur ärendets data (serverns namngivna tal)
 *  och annars ur resorna. null = okänd typ (då visas serverns text). */
export function explainAnomaly(item: ReviewItem): Explanation | null {
  const d = item.data;
  const two = isTwoTrips(item);
  const one = !two ? anomalyTrips(item)[0] : undefined;
  const a = two ? (item.first_trip ?? undefined) : undefined;
  const b = two ? (item.second_trip ?? undefined) : undefined;
  const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  const clock = (iso: string | null | undefined) => (iso ? formatTime(iso) : "okänd tid");
  const at = (iso: string | null | undefined) => (iso ? `kl. ${formatTime(iso)}` : "vid en okänd tid");
  const minutes = (m: number) => formatDuration(Math.abs(m) * 60);
  // Råresan är bara en del av resan i rutan (sammanslagen resa): säg vilken del det gäller.
  const partStart = str(d.part_started_at);
  const partEnd = str(d.part_ended_at);
  const part = d.part_of_trip === true ? `Det gäller en delsträcka av resan${partStart && partEnd ? ` (${clock(partStart)}–${clock(partEnd)})` : ""}. ` : "";
  const tripKm = num(d.trip_distance_km);
  const timesOverlap = (x: Trip, y: Trip) => !!(x.ended_at && y.started_at && Date.parse(y.started_at) < Date.parse(x.ended_at));

  switch (anomalyType(item)) {
    case "odometer_mismatch": {
      // Delsträckans värden när råresan bara är en del av resan; underlaget redovisar hela resan.
      const dist = num(d.part_distance_km) ?? num(d.distance_km) ?? num(d.reported_km) ?? one?.distance_km;
      const reported = part ? (tripKm ?? one?.distance_km) : dist;
      const start = num(d.odometer_start) ?? (part ? undefined : (one?.odometer_start ?? undefined));
      const end = num(d.odometer_end) ?? (part ? undefined : (one?.odometer_end ?? undefined));
      const odo = num(d.odometer_km) ?? (start != null && end != null ? end - start : undefined);
      const range = start != null && end != null ? ` (från ${formatOdometer(start)} till ${formatOdometer(end)})` : "";
      const diff = num(d.difference_km) ?? (dist != null && odo != null ? odo - dist : undefined);
      const ifOdoRight =
        diff != null && Math.abs(diff) >= 0.05
          ? diff > 0
            ? ` Stämmer mätarställningen saknas då ${formatKm(diff)}.`
            : ` Stämmer mätarställningen redovisas då ${formatKm(-diff)} för mycket.`
          : "";
      return {
        problem:
          part +
          (dist != null && odo != null
            ? `${part ? "Delsträckans" : "Resans"} sträcka är ${formatKm(dist)}, men mätarställningen ökade ${formatKm(odo, 0)} under ${part ? "den" : "resan"}${range}.`
            : "Resans sträcka och mätarställningens ökning under resan skiljer sig åt."),
        basis: part
          ? `Resans sträcka${reported != null ? `, ${formatKm(reported)}` : ""}, där delsträckan ingår${dist != null ? ` med ${formatKm(dist)}` : ""}. Mätarställningen används inte för sträckan.`
          : `Resans sträcka${dist != null ? `, ${formatKm(dist)}` : ""}. Mätarställningen används inte för sträckan.`,
        action: fixDistance(one),
        ifNothing: `Ärendet ligger kvar och underlaget redovisar ${reported != null ? formatKm(reported) : "resans sträcka"}${one && one.category !== "work" ? " om resan blir en tjänsteresa" : ""}.${ifOdoRight}`,
      };
    }
    case "odometer_backwards": {
      if (two) {
        const firstEnd = num(d.first_odometer_end) ?? a?.odometer_end ?? undefined;
        const secondStart = num(d.second_odometer_start) ?? b?.odometer_start ?? undefined;
        return {
          problem:
            firstEnd != null && secondStart != null
              ? `Den andra resan börjar på mätarställningen ${formatOdometer(secondStart)}, men den första slutade på ${formatOdometer(firstEnd)}. Mätarställningen går bakåt mellan resorna, så de kan överlappa eller vara samma körning två gånger.`
              : "Mätarställningen går bakåt mellan två resor, så de kan överlappa eller vara samma körning två gånger.",
          basis: a && b ? `Båda resorna, med var sin sträcka (${formatKm(a.distance_km)} och ${formatKm(b.distance_km)}).` : "Båda resorna, med var sin sträcka.",
          action: `Öppna resorna och jämför tider och platser. ${FIX_COPY} Stämmer båda: välj Avfärda.`,
          ifNothing: "Båda resorna redovisas som de står. Är de samma körning räknas sträckan två gånger.",
        };
      }
      const start = num(d.odometer_start) ?? (part ? undefined : (one?.odometer_start ?? undefined));
      const end = num(d.odometer_end) ?? (part ? undefined : (one?.odometer_end ?? undefined));
      const dist = part ? (tripKm ?? one?.distance_km) : (num(d.reported_km) ?? num(d.distance_km) ?? one?.distance_km);
      const who = part ? "delsträckan" : "resan";
      return {
        problem:
          part +
          (start != null && end != null
            ? `Mätarställningen är lägre när ${who} slutar (${formatOdometer(end)}) än när den börjar (${formatOdometer(start)}).`
            : `Mätarställningen är lägre när ${who} slutar än när den börjar.`),
        basis: `Resans sträcka${dist != null ? `, ${formatKm(dist)}` : ""}. Mätarställningarna står i underlaget som de är.`,
        action: fixDistance(one),
        ifNothing: "Ärendet ligger kvar, och mätarställningarna står kvar så i underlaget.",
      };
    }
    case "possible_duplicate": {
      const km1 = num(d.first_distance_km) ?? a?.distance_km;
      const km2 = num(d.second_distance_km) ?? b?.distance_km;
      return {
        problem: `Två resor startar nästan samtidigt${a && b ? ` (${at(a.started_at)} och ${at(b.started_at)})` : ""} och är lika långa${km1 != null && km2 != null ? ` (${formatKm(km1)} och ${formatKm(km2)})` : ""}. Den ena kan vara en kopia av den andra.`,
        basis: "Båda resorna. Är båda tjänsteresor redovisas samma körning två gånger.",
        action: `Öppna resorna och jämför. ${FIX_COPY} Är det två olika resor: välj Avfärda.`,
        ifNothing: "Båda resorna står kvar och redovisas om de är tjänsteresor.",
      };
    }
    case "time_conflict": {
      const startedAt = str(d.started_at) ?? one?.started_at ?? null;
      const endedAt = str(d.ended_at) ?? one?.ended_at ?? null;
      const span = startedAt || endedAt ? `${clock(startedAt)}–${clock(endedAt)}` : null;
      const fromTimes = secondsBetween(startedAt, endedAt);
      const elapsedMin = num(d.elapsed_minutes) ?? (fromTimes != null ? fromTimes / 60 : undefined);
      const drivingMin = num(d.driving_minutes) ?? (one?.driving_seconds != null ? one.driving_seconds / 60 : undefined);
      let problem = `${part}Resans körtid och klockslag går inte ihop.`;
      if (elapsedMin != null && elapsedMin < 0) problem = `${part}Resan slutar ${at(endedAt)} men börjar ${at(startedAt)}, alltså ${minutes(elapsedMin)} innan den har börjat.`;
      else if (elapsedMin != null && drivingMin != null && drivingMin > elapsedMin)
        problem = `${part}Resan har ${minutes(drivingMin)} körtid, men mellan start ${at(startedAt)} och ankomst ${at(endedAt)} gick bara ${minutes(elapsedMin)}. Någon av tiderna stämmer inte.`;
      return {
        problem,
        basis: span ? `Klockslagen ${span}. Körtiden står inte i underlaget.` : "Resans klockslag. Körtiden står inte i underlaget.",
        action: fixTimes(one),
        ifNothing: span ? `Underlaget visar klockslagen ${span}.` : "Underlaget visar resans klockslag som de står.",
      };
    }
    case "overlap": {
      // Sammanslagningens äldre ärende (före 1.3.2) kunde gälla resor som bara överlappar i
      // mätarställning: förklara det då som det är, inte som ett överlapp i tid.
      if (!str(d.first_ended_at) && a && b && !timesOverlap(a, b) && a.odometer_end != null && b.odometer_start != null && b.odometer_start < a.odometer_end) {
        return explainAnomaly({ ...item, data: { ...d, type: "odometer_backwards" } });
      }
      const firstEnd = str(d.first_ended_at) ?? a?.ended_at ?? null;
      const secondStart = str(d.second_started_at) ?? b?.started_at ?? null;
      return {
        problem: firstEnd || secondStart ? `Den första resan slutar ${at(firstEnd)}, men nästa börjar redan ${at(secondStart)}. Resorna överlappar i tid.` : "Två resor överlappar varandra i tid.",
        basis: "Båda resorna, med sina klockslag och sträckor.",
        action: `Öppna resorna och jämför. ${FIX_COPY} ${fixTimes(b)}`,
        ifNothing: "Båda resorna redovisas som de står. Är de samma körning räknas sträckan två gånger.",
      };
    }
    default:
      return null;
  }
}

function TripBox({ trip, label }: { trip: Trip; label?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-surface-2/50 p-4" data-testid="anomaly-trip" data-trip-id={trip.id}>
      {label ? <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">{label}</p> : null}
      <TripSummary trip={trip} showPurpose />
    </div>
  );
}

/** Kortets innehåll för en avvikelse: resan, förklaringen och knapparna. */
export function AnomalyBody({ item, onResolve, onClick }: { item: ReviewItem; onResolve: () => void; onClick: (e: React.MouseEvent, fn: () => void) => void }) {
  const type = item.data.type ?? "";
  const tripBound = isTripBound(item);
  const two = tripBound && isTwoTrips(item);
  const trips = anomalyTrips(item);
  const missing = tripBound && (two ? trips.length < 2 : trips.length < 1);
  // Saknas resan går det inte att säga vad man gör med den: bara meddelandet och Avfärda (raden nedanför).
  const explanation = tripBound && !missing ? explainAnomaly(item) : null;
  const vehicleLink = VEHICLE_LINKS[type];
  const date = missing ? anomalyDate(item) : null;

  return (
    <div className="space-y-5" data-testid="anomaly-body" data-anomaly-type={type}>
      {tripBound ? (
        trips.length ? (
          two ? (
            <div className="grid gap-3 md:grid-cols-2">
              {item.first_trip ? <TripBox trip={item.first_trip} label="Första resan" /> : null}
              {item.second_trip ? <TripBox trip={item.second_trip} label="Andra resan" /> : null}
            </div>
          ) : (
            <TripBox trip={trips[0]} />
          )
        ) : null
      ) : null}
      {missing ? (
        <p className="flex items-start gap-2 rounded-lg border border-border bg-surface-2/50 p-3 text-sm text-fg-muted" data-testid="anomaly-trip-missing">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            {two && trips.length === 1 ? "Den ena resan" : two ? "Resorna" : "Resan"}
            {date ? ` (${date})` : ""} kunde inte visas. Den kan ha tagits bort eller ersatts av en import – då stängs ärendet av sig självt. Du kan också välja Avfärda.
          </span>
        </p>
      ) : null}

      {explanation ? (
        <dl className="space-y-3 rounded-lg border border-border p-4 text-sm" data-testid="anomaly-explanation">
          {(
            [
              ["Vad som inte stämmer", explanation.problem],
              ["I underlaget", explanation.basis],
              ["Vad du gör", explanation.action],
              ["Om du inte gör något", explanation.ifNothing],
            ] as const
          ).map(([term, text]) => (
            <div key={term} className="grid gap-0.5 sm:grid-cols-[150px_minmax(0,1fr)] sm:gap-3">
              <dt className="font-medium text-fg-muted">{term}</dt>
              <dd className="min-w-0 break-words">{text}</dd>
            </div>
          ))}
        </dl>
      ) : item.detail && !missing ? (
        <p className="text-sm text-fg-muted">{item.detail}</p>
      ) : null}

      {missing ? null : (
        <>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            {two
              ? [item.first_trip, item.second_trip].map((t, i) =>
                  t ? (
                    <Button key={t.id} size="lg" variant="primary" asChild>
                      <Link href={`/journal/${t.id}`} data-testid={i === 0 ? "anomaly-open-trip" : "anomaly-open-trip-2"}>
                        <ExternalLink aria-hidden /> {i === 0 ? "Öppna första resan" : "Öppna andra resan"}
                      </Link>
                    </Button>
                  ) : null,
                )
              : trips[0] ? (
                  <Button size="lg" variant="primary" asChild>
                    <Link href={`/journal/${trips[0].id}`} data-testid="anomaly-open-trip">
                      <ExternalLink aria-hidden /> Öppna resan
                    </Link>
                  </Button>
                ) : null}
            {vehicleLink ? (
              <Button size="lg" variant="primary" asChild>
                <Link href={vehicleLink.href} data-testid="anomaly-open-place">
                  <ExternalLink aria-hidden /> {vehicleLink.label}
                </Link>
              </Button>
            ) : null}
            <Button
              size="lg"
              variant={trips.length || vehicleLink ? "secondary" : "primary"}
              onClick={(e) => onClick(e, onResolve)}
              data-testid="anomaly-resolve"
            >
              <Check aria-hidden /> Markera som hanterat
            </Button>
          </div>
          <p className={cn("flex items-start gap-2 text-xs text-fg-muted")} data-testid="anomaly-help">
            <Info className="mt-px size-3.5 shrink-0" aria-hidden />
            <span>
              <span className="font-medium text-fg">Markera som hanterat</span> när du har rättat det. <span className="font-medium text-fg">Avfärda</span> när uppgifterna
              stämmer som de är. Båda stänger ärendet, och samma avvikelse visas inte igen.
            </span>
          </p>
        </>
      )}
    </div>
  );
}
