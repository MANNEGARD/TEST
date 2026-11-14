"use client";

import * as React from "react";
import { Map as MapIcon } from "lucide-react";
import { PointMap, type MapPoint } from "@/components/map/point-map";
import { useMapSettings } from "@/components/map/use-map-settings";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { formatNumber, plural } from "@/lib/format";
import type { Place } from "@/lib/places-types";
import { typeLabel } from "./location-type";

/**
 * Karta över platser som har en position. Hopfälld som standard och visas bara när
 * minst en plats har position och kartor är påslagna – kartplattor hämtas först
 * när sektionen öppnas.
 */
export function PlacesMap({ places }: { places: Place[] }) {
  const settings = useMapSettings();
  const [open, setOpen] = React.useState(false);
  const points = React.useMemo<MapPoint[]>(
    () =>
      places
        .filter((p) => p.latitude != null && p.longitude != null)
        .map((p) => ({
          lat: p.latitude as number,
          lon: p.longitude as number,
          kind: "position",
          label: p.name,
          detail: [typeLabel(p.type), plural(p.visit_count, "besök", "besök")].join(" · "),
        })),
    [places],
  );
  if (!points.length || !settings.enabled || !settings.tilesUrl) return null;

  return (
    <CollapsibleSection
      className="mt-5"
      icon={<MapIcon />}
      title="Karta"
      meta={`${formatNumber(points.length, 0)} av ${plural(places.length, "plats har position", "platser har position")}`}
      onToggle={setOpen}
    >
      <div className="h-72 sm:h-96">{open ? <PointMap points={points} tilesUrl={settings.tilesUrl} ariaLabel="Karta över platser med position" /> : null}</div>
    </CollapsibleSection>
  );
}
