"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { CalendarCheck, Layers } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { SortableTH, Table, TBody, TD, TFoot, TH, THead, TR } from "@/components/ui/table";
import { Tooltip } from "@/components/ui/tooltip";
import { formatDate, formatKm, formatOdometer, formatWeekday, plural } from "@/lib/format";
import type { CategoryCode, Trip, TripList } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AddressCell } from "./address";
import { AiStatus } from "./ai-status";
import { CategoryPill } from "./category-pill";
import { CategoryToggle } from "./category-toggle";
import { FavoriteStar } from "./favorite-star";
import { InlinePurpose } from "./inline-purpose";
import { QualityValue } from "./quality-value";
import { ReviewStatusBadge } from "./review-status-badge";
import { SourceBadge } from "./source-badge";
import { TRIP_COLUMNS } from "./trip-columns";
import { TripTime } from "./trip-time";

export interface TripRowHandlers {
  onCategory: (trip: Trip, c: CategoryCode) => void;
  onPurpose: (trip: Trip, text: string) => void;
  onFavorite: (trip: Trip) => void;
}

export function distanceQualityNote(trip: Trip): string | undefined {
  if (trip.quality_flags.includes("distance_estimated")) return "Sträckan är uppskattad (inte uppmätt av bilen).";
  return undefined;
}

/** Desktop-tabell för körjournalen. Sortering och urval sker på servern. */
export function TripTable({
  data,
  visible,
  sort,
  order,
  onSort,
  selected,
  onSelect,
  handlers,
}: {
  data: TripList;
  visible: string[];
  sort: string;
  order: "asc" | "desc";
  onSort: (field: string) => void;
  selected: Set<string>;
  onSelect: (ids: string[], on: boolean) => void;
  handlers: TripRowHandlers;
}) {
  const router = useRouter();
  const cols = TRIP_COLUMNS.filter((c) => visible.includes(c.id));
  const allOnPage = data.items.length > 0 && data.items.every((t) => selected.has(t.id));
  const someOnPage = data.items.some((t) => selected.has(t.id));

  const cell = (t: Trip, id: string) => {
    switch (id) {
      case "date":
        return (
          <span className="whitespace-nowrap">
            <span className="mr-1.5 text-xs text-fg-subtle">{formatWeekday(t.started_at ?? t.sort_time, true)}</span>
            <span className="text-[13px] font-medium">{formatDate(t.started_at ?? t.sort_time)}</span>
          </span>
        );
      case "start_time":
        return <span className="text-[13px]"><TripTime at={t.started_at} quality={t.start_time_quality} /></span>;
      case "end_time":
        return <span className="text-[13px]"><TripTime at={t.ended_at} quality={t.end_time_quality} /></span>;
      case "start_address":
        return <AddressCell address={t.start_address} location={t.start_location} className="block max-w-[170px] truncate text-[13px]" />;
      case "end_address":
        return (
          <span className="flex max-w-[190px] items-center gap-1.5 text-[13px]">
            <AddressCell address={t.end_address} location={t.end_location} className="truncate" />
            {t.segment_count > 1 ? (
              <Tooltip content={`Sammanslagen av ${t.segment_count} delresor`}>
                <span tabIndex={0} className="inline-flex shrink-0 items-center gap-0.5 rounded-sm bg-surface-2 px-1 text-[11px] text-fg-muted">
                  <Layers className="size-3" aria-hidden />
                  {t.segment_count}
                </span>
              </Tooltip>
            ) : null}
          </span>
        );
      case "odometer_start":
        return <QualityValue value={t.odometer_start != null ? formatOdometer(t.odometer_start) : null} mono missingText="Mätarställning saknas" className="text-[13px]" />;
      case "odometer_end":
        return <QualityValue value={t.odometer_end != null ? formatOdometer(t.odometer_end) : null} mono missingText="Mätarställning saknas" className="text-[13px]" />;
      case "distance":
        return (
          <span className="inline-flex items-center gap-1">
            <QualityValue
              value={formatKm(t.distance_km)}
              quality={t.quality_flags.includes("distance_estimated") ? "estimated" : "actual"}
              explanation={distanceQualityNote(t)}
              className="text-[13px] font-medium"
            />
          </span>
        );
      case "category":
        return (
          <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
            {t.has_mixed_categories ? (
              <CategoryPill category={t.category} mixed size="sm" />
            ) : (
              <CategoryToggle value={t.category} onChange={(c) => handlers.onCategory(t, c)} tripLabel={`resan ${formatDate(t.started_at ?? t.sort_time)}`} />
            )}
          </div>
        );
      case "purpose":
        return (
          <span className="flex max-w-[220px] items-center gap-1.5">
            <InlinePurpose value={t.purpose} confirmed={t.purpose_confirmed} onSave={(text) => handlers.onPurpose(t, text)} />
            {t.has_calendar_match ? (
              <Tooltip content="Matchar ett möte i kalendern">
                <CalendarCheck tabIndex={0} className="size-3.5 shrink-0 text-accent" aria-label="Kalendermatchning" />
              </Tooltip>
            ) : null}
          </span>
        );
      case "ai":
        return <AiStatus trip={t} />;
      case "source":
        return <SourceBadge source={t.source} short />;
      case "status":
        return <ReviewStatusBadge status={t.review_status} short />;
      default:
        return null;
    }
  };

  return (
    <Table wrapperClassName="rounded-b-lg" aria-label="Resor">
      <THead>
        <tr>
          <TH className="w-10 pr-0">
            <Checkbox
              checked={allOnPage ? true : someOnPage ? "indeterminate" : false}
              onCheckedChange={(v) => onSelect(data.items.map((t) => t.id), v === true)}
              aria-label="Markera alla resor på sidan"
            />
          </TH>
          <TH className="w-8 px-0">
            <span className="sr-only">Favorit</span>
          </TH>
          {cols.map((c) =>
            c.sort ? (
              <SortableTH key={c.id} label={c.label} field={c.sort} sort={sort} order={order} onSort={onSort} align={c.align} />
            ) : (
              <TH key={c.id} className={cn(c.align === "right" && "text-right")}>
                {c.label}
              </TH>
            ),
          )}
        </tr>
      </THead>
      <TBody>
        {data.items.map((t) => {
          const isSel = selected.has(t.id);
          return (
            <TR
              key={t.id}
              data-selected={isSel}
              data-testid="trip-row"
              className="group cursor-pointer"
              onClick={(e) => {
                if ((e.target as HTMLElement).closest("button,input,a,[role=checkbox],[role=radio]")) return;
                router.push(`/journal/${t.id}`);
              }}
            >
              <TD className="w-10 pr-0" onClick={(e) => e.stopPropagation()}>
                <Checkbox checked={isSel} onCheckedChange={(v) => onSelect([t.id], v === true)} aria-label={`Markera resan ${formatDate(t.started_at ?? t.sort_time)}`} />
              </TD>
              <TD className="w-8 px-0">
                <FavoriteStar active={t.is_favorite} onToggle={() => handlers.onFavorite(t)} />
              </TD>
              {cols.map((c) => (
                <TD key={c.id} className={cn(c.align === "right" && "text-right", c.id === "date" && "py-2.5")}>
                  {c.id === "date" ? (
                    <a href={`/journal/${t.id}`} onClick={(e) => { e.preventDefault(); router.push(`/journal/${t.id}`); }} className="rounded-sm hover:underline focus-visible:outline-2 focus-visible:outline-ring">
                      {cell(t, c.id)}
                    </a>
                  ) : (
                    cell(t, c.id)
                  )}
                </TD>
              ))}
            </TR>
          );
        })}
      </TBody>
      <TFoot>
        <tr>
          <TD colSpan={2} />
          <TD colSpan={cols.length} className="py-2.5">
            <TotalsLine total={data.total} totals={data.totals} />
          </TD>
        </tr>
      </TFoot>
    </Table>
  );
}

/** Summering av hela urvalet (alla sidor) per kategori, från `totals`. */
export function TotalsLine({ total, totals, className }: { total: number; totals: Record<string, number>; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px]", className)}>
      <span className="text-fg-muted">
        Summa för {plural(total, "resa", "resor")}:{" "}
        <strong className="font-mono font-semibold text-fg">{formatKm(totals.distance_km)}</strong>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="size-2 rounded-full bg-work" aria-hidden />
        Arbete <span className="font-mono">{formatKm(totals.work_km)}</span>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="size-2 rounded-full bg-private" aria-hidden />
        Privat <span className="font-mono">{formatKm(totals.private_km)}</span>
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="size-2 rounded-full bg-undecided" aria-hidden />
        Ej avgjord <span className="font-mono">{formatKm(totals.undecided_km)}</span>
      </span>
    </div>
  );
}
