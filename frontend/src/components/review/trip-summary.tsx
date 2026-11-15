import { ArrowRight } from "lucide-react";
import { CategoryPill } from "@/components/trips/category-pill";
import { QualityValue } from "@/components/trips/quality-value";
import { TripTime } from "@/components/trips/trip-time";
import { formatDate, formatKm, formatWeekday } from "@/lib/format";
import type { Trip } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Kompakt sammanfattning av en resa (granskning, sammanslagningsfrågor). */
export function TripSummary({ trip: t, className, showCategory = true }: { trip: Trip; className?: string; showCategory?: boolean }) {
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
        <span className="truncate">{t.start_location?.confirmed ? t.start_location.name : (t.start_address ?? "Okänd start")}</span>
        <ArrowRight className="size-4 shrink-0 text-fg-subtle" aria-label="till" />
        <span className="truncate">{t.end_location?.confirmed ? t.end_location.name : (t.end_address ?? "Okänt mål")}</span>
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <span className="num font-medium">
          <QualityValue value={formatKm(t.distance_km)} quality={t.quality_flags.includes("distance_estimated") ? "estimated" : "actual"} />
        </span>
        {showCategory ? <CategoryPill category={t.category} mixed={t.has_mixed_categories} preliminary={t.review_status !== "approved" && t.review_status !== "imported"} size="sm" /> : null}
      </div>
    </div>
  );
}
