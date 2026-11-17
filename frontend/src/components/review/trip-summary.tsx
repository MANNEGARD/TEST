import { ArrowRight } from "lucide-react";
import { CategoryPill } from "@/components/trips/category-pill";
import { QualityValue } from "@/components/trips/quality-value";
import { TripTime } from "@/components/trips/trip-time";
import { formatDate, formatKm, formatWeekday } from "@/lib/format";
import type { Trip } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Platsens namn (bekräftad plats) eller adressen, som resan visas i granskningen. */
export function tripPlace(t: Trip, end: "start" | "end"): string {
  const loc = end === "start" ? t.start_location : t.end_location;
  const address = end === "start" ? t.start_address : t.end_address;
  return loc?.confirmed ? loc.name : (address ?? (end === "start" ? "Okänd start" : "Okänt mål"));
}

/** Kompakt sammanfattning av en resa (granskning, sammanslagningsfrågor). */
export function TripSummary({
  trip: t,
  className,
  showCategory = true,
  showPurpose = false,
}: {
  trip: Trip;
  className?: string;
  showCategory?: boolean;
  /** Visa resans ärende (syfte) under sträckan. */
  showPurpose?: boolean;
}) {
  const when = t.started_at ?? t.sort_time;
  return (
    <div className={cn("min-w-0", className)}>
      <p className="text-xs text-fg-muted">
        <span className="capitalize">{formatWeekday(when)}</span> <span className="num">{formatDate(when)}</span>
        <span className="mx-1.5">·</span>
        <span className="num">
          <TripTime at={t.started_at} quality={t.start_time_quality} />–<TripTime at={t.ended_at} quality={t.end_time_quality} />
        </span>
      </p>
      <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-[15px] font-medium">
        <span className="min-w-0 break-words">{tripPlace(t, "start")}</span>
        <ArrowRight className="size-4 shrink-0 text-fg-subtle" aria-label="till" />
        <span className="min-w-0 break-words">{tripPlace(t, "end")}</span>
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <span className="num font-medium">
          <QualityValue value={formatKm(t.distance_km)} quality={t.quality_flags.includes("distance_estimated") ? "estimated" : "actual"} />
        </span>
        {showCategory ? <CategoryPill category={t.category} mixed={t.has_mixed_categories} preliminary={t.review_status !== "approved" && t.review_status !== "imported"} size="sm" /> : null}
      </div>
      {showPurpose ? (
        <p className="mt-1.5 text-sm" data-testid="trip-summary-purpose">
          <span className="text-fg-muted">Ärende: </span>
          {t.purpose ? <span className="break-words">{t.purpose}</span> : <span className="text-fg-muted">{t.category === "private" ? "– (privat resa)" : "inget angivet"}</span>}
        </p>
      ) : null}
    </div>
  );
}
