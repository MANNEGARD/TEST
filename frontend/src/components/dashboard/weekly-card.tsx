"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, CalendarRange, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatKm, formatMil, formatNumber, localDateISO, plural } from "@/lib/format";
import { useWeeklyReport } from "@/lib/queries/dashboard";

/** yyyy-MM-dd ± dagar (kalenderdatum, ingen tidszon inblandad). */
function shiftDate(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/**
 * "Veckan i korthet" – veckorapporten (GET /reports/weekly) som ett hopfällt kort på översikten.
 * När endast tjänsteresor redovisas nämns inga luckor i mätarställningen (de är privat körning).
 */
export function WeeklyCard() {
  // undefined = innevarande vecka; annars måndagen i en tidigare vecka.
  const [weekStart, setWeekStart] = React.useState<string | undefined>(undefined);
  const q = useWeeklyReport(weekStart);
  const r = q.data;
  const isCurrent = weekStart === undefined;
  const meta = r ? `v. ${r.week.week} · ${formatMil(r.distance.work.km)} i tjänsten` : undefined;

  return (
    <div className="mt-6">
      <CollapsibleSection title="Veckan i korthet" icon={<CalendarRange />} meta={meta}>
        {q.isError && !r ? (
          <ErrorState error={q.error} onRetry={() => q.refetch()} />
        ) : !r ? (
          <div className="space-y-3" role="status" aria-label="Laddar veckan">
            <Skeleton className="h-5 w-48" />
            <Skeleton className="h-14" />
          </div>
        ) : (
          <div className={q.isPlaceholderData ? "space-y-4 opacity-60 transition-opacity" : "space-y-4"}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm">
                <span className="font-medium">Vecka {r.week.week}</span>
                <span className="ml-2 num text-xs text-fg-muted">
                  {formatDate(r.week.start)} – {formatDate(r.week.end)}
                </span>
              </p>
              <div className="flex items-center gap-1">
                <Button size="icon-xs" variant="ghost" aria-label="Föregående vecka" onClick={() => setWeekStart(shiftDate(r.week.start, -7))}>
                  <ChevronLeft aria-hidden />
                </Button>
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label="Nästa vecka"
                  disabled={isCurrent}
                  onClick={() => {
                    const next = shiftDate(r.week.start, 7);
                    // Tillbaka till innevarande vecka när nästa vecka är den som pågår.
                    setWeekStart(next > shiftDate(localDateISO(), -7) ? undefined : next);
                  }}
                >
                  <ChevronRight aria-hidden />
                </Button>
              </div>
            </div>

            {r.trips.total === 0 ? (
              <p className="text-sm text-fg-muted">Inga resor den här veckan.</p>
            ) : (
              <>
                <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Fact label="Tjänstemil" value={formatMil(r.distance.work.km)} sub={formatKm(r.distance.work.km)} />
                  <Fact label="Resor" value={formatNumber(r.trips.total, 0)} />
                  <Fact label="Väntar på granskning" value={formatNumber(r.unreviewed_trips, 0)} />
                  <Fact label="Utan syfte" value={formatNumber(r.missing_purpose_trips, 0)} />
                </dl>
                {r.top_destinations.length ? (
                  <div>
                    <h3 className="mb-1.5 text-xs font-medium text-fg-muted">Vanligaste destinationerna</h3>
                    <ol className="space-y-1 text-sm">
                      {r.top_destinations.slice(0, 3).map((d) => (
                        <li key={d.label} className="flex items-baseline justify-between gap-3">
                          <span className="truncate" title={d.label}>
                            {d.label}
                          </span>
                          <span className="shrink-0 num text-xs text-fg-muted">
                            {plural(d.trips, "resa", "resor")} · {formatKm(d.total_km)}
                          </span>
                        </li>
                      ))}
                    </ol>
                  </div>
                ) : null}
                {r.journal_scope !== "business_only" && r.data_gaps.count > 0 ? (
                  <p className="text-xs text-fg-muted">
                    {plural(r.data_gaps.count, "lucka", "luckor")} i mätarställningen ({formatKm(r.data_gaps.km)}).
                  </p>
                ) : null}
              </>
            )}

            {r.trips_to_review > 0 ? (
              <Button asChild size="sm" variant="secondary">
                <Link href="/review">
                  Granska {plural(r.trips_to_review, "resa", "resor")} <ArrowRight aria-hidden />
                </Link>
              </Button>
            ) : r.trips.total > 0 ? (
              <p className="text-xs text-ok-ink">Veckans resor är granskade.</p>
            ) : null}
          </div>
        )}
      </CollapsibleSection>
    </div>
  );
}

function Fact({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="mt-0.5 text-lg font-medium num">
        {value}
        {sub ? <span className="ml-1.5 text-xs font-normal text-fg-muted">{sub}</span> : null}
      </dd>
    </div>
  );
}
