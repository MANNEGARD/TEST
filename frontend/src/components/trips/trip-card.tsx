"use client";

import Link from "next/link";
import { ArrowDown, Layers } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { formatDate, formatKm, formatWeekday } from "@/lib/format";
import type { Trip } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AddressCell } from "./address";
import { CategoryPill } from "./category-pill";
import { CategoryToggle } from "./category-toggle";
import { FavoriteStar } from "./favorite-star";
import { InlinePurpose } from "./inline-purpose";
import { QualityValue } from "./quality-value";
import { ReviewStatusBadge } from "./review-status-badge";
import { SourceBadge } from "./source-badge";
import type { TripRowHandlers } from "./trip-table";
import { distanceQualityNote } from "./trip-table";
import { listStartBounds, TripTime } from "./trip-time";

/** Resa som kort (mobilvy av körjournalen). */
export function TripCard({
  trip: t,
  selected,
  onSelect,
  handlers,
}: {
  trip: Trip;
  selected: boolean;
  onSelect: (on: boolean) => void;
  handlers: TripRowHandlers;
}) {
  const when = t.started_at ?? t.sort_time;
  return (
    <article
      data-testid="trip-card"
      className={cn("rounded-lg border border-border bg-surface p-3.5 shadow-sm", selected && "border-accent/60 bg-accent-soft/40")}
    >
      <header className="flex items-center gap-2">
        <Checkbox checked={selected} onCheckedChange={(v) => onSelect(v === true)} aria-label={`Markera resan ${formatDate(when)}`} />
        <Link href={`/journal/${t.id}`} className="flex min-w-0 flex-1 items-baseline gap-1.5 overflow-hidden whitespace-nowrap rounded-sm text-xs focus-visible:outline-2 focus-visible:outline-ring">
          <span className="text-fg-subtle">{formatWeekday(when, true)}</span>
          <span className="font-medium text-fg num">{formatDate(when)}</span>
          <span className="truncate text-fg-muted num">
            <TripTime at={t.started_at} quality={t.start_time_quality} {...listStartBounds(t)} />–<TripTime at={t.ended_at} quality={t.end_time_quality} />
          </span>
        </Link>
        <span className="shrink-0 num text-[15px] font-semibold">
          <QualityValue value={formatKm(t.distance_km)} quality={t.quality_flags.includes("distance_estimated") ? "estimated" : "actual"} explanation={distanceQualityNote(t)} />
        </span>
        <FavoriteStar active={t.is_favorite} onToggle={() => handlers.onFavorite(t)} className="-mr-1" />
      </header>
      <Link href={`/journal/${t.id}`} className="mt-2.5 block space-y-0.5 rounded-sm pl-6 text-[13px] focus-visible:outline-2 focus-visible:outline-ring">
        <div className="truncate text-fg-muted"><AddressCell address={t.start_address} location={t.start_location} /></div>
        <ArrowDown className="size-3 text-fg-subtle" aria-hidden />
        <div className="flex items-center gap-1.5 truncate font-medium">
          <AddressCell address={t.end_address} location={t.end_location} className="truncate" />
          {t.segment_count > 1 ? (
            <span className="inline-flex items-center gap-0.5 rounded-sm bg-surface-2 px-1 text-[11px] font-normal text-fg-muted">
              <Layers className="size-3" aria-hidden /> {t.segment_count}
            </span>
          ) : null}
        </div>
      </Link>
      <div className="mt-3 flex flex-wrap items-center gap-2 pl-6">
        {t.has_mixed_categories ? (
          <CategoryPill category={t.category} mixed />
        ) : (
          <CategoryToggle value={t.category} onChange={(c) => handlers.onCategory(t, c)} size="md" />
        )}
        <ReviewStatusBadge status={t.review_status} short approvalSource={t.approval_source} approvalReason={t.approval_reason} />
        {/* "Importerad" + "Importerad" säger samma sak två gånger – källan visas bara när den tillför något. */}
        {t.review_status === "imported" && t.source === "volvo_import" ? null : <SourceBadge source={t.source} short />}
      </div>
      <div className="mt-2 pl-5">
        <InlinePurpose value={t.purpose} confirmed={t.purpose_confirmed} onSave={(text) => handlers.onPurpose(t, text)} />
      </div>
    </article>
  );
}
