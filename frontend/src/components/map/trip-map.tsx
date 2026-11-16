"use client";

import Link from "next/link";
import { Info } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, formatOdometer } from "@/lib/format";
import type { TripDetail } from "@/lib/types";
import { MapPlaceholder } from "./map-placeholder";
import { PointMap, type MapPoint } from "./point-map";
import { useMapSettings } from "./use-map-settings";

const BASIS_TEXT: Record<string, string> = {
  vehicle_position: "Fordonets position",
  known_location: "Position från känd plats",
  address_lookup: "Ungefärlig position ur adressen",
};

/**
 * Varför kartan saknar punkter (1.3.0): platsens adress slås upp i bakgrunden, eller kartan
 * hittade inte adressen. null = inget adressuppslag (geokodningen är av eller ingen adress).
 */
function lookupState(stops: TripDetail["map"]["stops"]): { kind: "pending" | "failed"; failed: number } | null {
  const states = stops.filter((s) => s.lat == null || s.lon == null).map((s) => s.position_lookup ?? s.location?.position_lookup ?? null);
  if (states.includes("pending")) return { kind: "pending", failed: 0 };
  const failed = states.filter((x) => x === "not_found" || x === "ambiguous").length;
  return failed ? { kind: "failed", failed } : null;
}

/** Resans karta: faktiska punkter (start, stopp, mål, positionsobservationer). */
export function TripMap({ map, className }: { map: TripDetail["map"]; className?: string }) {
  const settings = useMapSettings();
  const points: MapPoint[] = [];
  map.stops.forEach((s, i) => {
    if (s.lat == null || s.lon == null) return;
    const kind = i === 0 ? "start" : i === map.stops.length - 1 ? "end" : "stop";
    const name = s.location?.confirmed ? s.location.name : s.address ?? "Okänd adress";
    points.push({
      lat: s.lat,
      lon: s.lon,
      kind,
      label: `${kind === "start" ? "Start" : kind === "end" ? "Mål" : `Stopp ${i}`}: ${name}`,
      detail: [s.at ? formatDateTime(s.at) : null, BASIS_TEXT[s.coordinate_basis ?? "vehicle_position"] ?? BASIS_TEXT.vehicle_position]
        .filter(Boolean)
        .join(" · "),
    });
  });
  for (const o of map.observations) {
    points.push({
      lat: o.lat,
      lon: o.lon,
      kind: "observation",
      label: "Positionsobservation",
      detail: [formatDateTime(o.reported_at ?? o.fetched_at), o.odometer_km != null ? formatOdometer(o.odometer_km) : null].filter(Boolean).join(" · "),
    });
  }
  const connected = points.filter((p) => p.kind !== "observation").length >= 2;

  const lookup = !points.length && settings.enabled && settings.tilesUrl ? lookupState(map.stops) : null;
  let body: React.ReactNode;
  if (settings.loading) body = <Skeleton className="h-full min-h-64 w-full rounded-lg" />;
  else if (!settings.enabled || !settings.tilesUrl)
    body = <MapPlaceholder title="Kartor är avstängda" description="Kartvisning är avstängd i sekretessinställningarna. Inga kartplattor hämtas." className="min-h-64" />;
  else if (!points.length) {
    body =
      lookup?.kind === "pending" ? (
        <MapPlaceholder
          title="Positionen slås upp"
          description="Resan kommer från en importerad körjournal med adresser. DriveLog slår upp adresserna på kartan i bakgrunden, lite i taget – kartan visas när det är klart."
          className="min-h-64"
        />
      ) : lookup?.kind === "failed" ? (
        <MapPlaceholder
          title="Adressen hittades inte på kartan"
          description={`Kartan hittade inte ${lookup.failed > 1 ? "en av adresserna" : "adressen"} eller gav flera möjliga platser. Sätt ut platsen på kartan under Platser, så visas resan.`}
          className="min-h-64"
        />
      ) : (
        <MapPlaceholder
          title="Inga koordinater för resan"
          description="Resan kommer från en importerad körjournal med adresser men utan positioner. Bekräfta platserna under Platser för att se dem på kartan."
          className="min-h-64"
        />
      );
  }
  else body = <PointMap points={points} tilesUrl={settings.tilesUrl} connect={connected} ariaLabel="Karta över resans kända punkter" className="min-h-64" />;

  return (
    <div className={className}>
      <div className="h-64 sm:h-80">{body}</div>
      {lookup?.kind === "failed" ? (
        <p className="mt-2 text-xs">
          <Link href="/places" className="font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring">
            Öppna Platser
          </Link>
        </p>
      ) : null}
      {connected && settings.enabled ? (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-fg-muted">
          <Info className="mt-0.5 size-3.5 shrink-0 text-accent" aria-hidden />
          <span>
            <strong className="font-medium text-fg">Streckad linje = inte den faktiskt körda vägen.</strong> {map.route_note}
          </span>
        </p>
      ) : null}
    </div>
  );
}
