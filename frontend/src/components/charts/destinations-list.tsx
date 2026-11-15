"use client";

import { formatKm, formatMil, plural } from "@/lib/format";
import type { DestinationRow } from "@/lib/types";

/**
 * Körsträcka per destination som horisontella staplar (HTML – lättläst, inga
 * avklippta etiketter). Stapeln visar tjänstekörning; värdet står vid stapeln.
 */
export function DestinationsList({ items }: { items: DestinationRow[] }) {
  const max = Math.max(1, ...items.map((d) => d.total_km));
  return (
    <ol className="space-y-3">
      {items.map((d) => {
        const workPct = (d.work_km / max) * 100;
        const otherPct = ((d.total_km - d.work_km) / max) * 100;
        return (
          <li key={`${d.label}-${d.location_id ?? ""}`}>
            <div className="mb-1 flex items-baseline justify-between gap-3 text-sm">
              <span className="truncate" title={d.address ?? d.label}>
                {d.label}
              </span>
              <span className="shrink-0 num text-[13px] text-fg">
                {formatMil(d.total_km)}
                <span className="ml-1.5 font-sans text-xs text-fg-muted">{plural(d.trips, "resa", "resor")}</span>
              </span>
            </div>
            <div
              className="flex h-2 gap-0.5"
              role="img"
              aria-label={`${d.label}: ${formatKm(d.work_km)} arbete av ${formatKm(d.total_km)}`}
            >
              {workPct > 0 ? <span className="h-full rounded-r-[4px] bg-work" style={{ width: `${workPct}%` }} /> : null}
              {otherPct > 0.5 ? <span className="h-full rounded-r-[4px] bg-border-strong" style={{ width: `${otherPct}%` }} /> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
