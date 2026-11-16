import { ArrowDown } from "lucide-react";
import { CollectionGapNote } from "@/components/trip-detail/collection-gap-note";
import { CategoryPill } from "@/components/trips/category-pill";
import { TripTime } from "@/components/trips/trip-time";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatWeekday, milNumber } from "@/lib/format";
import type { QuestionTrip } from "@/lib/notifications-types";
import type { Trip } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Frågans platsetikett (nyare backend, aldrig en gatuadress). Reserv: platsens namn om den
 * är bekräftad, annars adressen (samma regel som i granskningen).
 */
function place(trip: QuestionTrip, full: Trip | undefined, end: "start" | "end"): string {
  const label = trip[`${end}_label`];
  if (label) return label;
  const loc = full?.[`${end}_location`];
  if (loc?.confirmed && loc.name) return loc.name;
  const address = end === "start" ? trip.start_address : trip.end_address;
  return address ?? (end === "start" ? "Okänd start" : "Okänt mål");
}

/**
 * Resan som frågan gäller: datum, från → till och sträcka i mil – stort och lätt att
 * läsa på telefonen. Frågans sammanfattning har platsetiketter och tidernas kvalitet; `full`
 * (resans detaljer) är reserv för äldre backend som saknar dem.
 */
export function QuickTripCard({
  trip,
  full,
  showCategory = true,
  showPurpose = true,
  className,
}: {
  trip: QuestionTrip;
  full?: Trip;
  showCategory?: boolean;
  /** Av i syftesfrågan, där syftesförslaget i stället blir en svarsknapp. */
  showPurpose?: boolean;
  className?: string;
}) {
  const when = trip.started_at ?? trip.ended_at;
  const proposal = trip.review_status === "proposed" || trip.review_status === "auto";
  const purpose = showPurpose ? trip.purpose : null;
  return (
    <div className={cn("rounded-xl border border-border bg-surface p-4 shadow-sm sm:p-5", className)}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-fg-muted">
          {when ? (
            <>
              <span className="capitalize">{formatWeekday(when)}</span> <span className="num">{formatDate(when)}</span>
            </>
          ) : (
            "Okänt datum"
          )}
          <span className="mx-1.5" aria-hidden>
            ·
          </span>
          <span className="num">
            <TripTime at={trip.started_at} quality={trip.start_time_quality ?? full?.start_time_quality ?? (trip.started_at ? "actual" : "unknown")} />–
            <TripTime at={trip.ended_at} quality={trip.end_time_quality ?? full?.end_time_quality ?? (trip.ended_at ? "actual" : "unknown")} />
          </span>
        </p>
        <p className="shrink-0 text-right">
          <span className="num text-2xl leading-none font-semibold tracking-tight text-fg">{milNumber(trip.distance_km)}</span>
          <span className="ml-1 text-sm text-fg-muted">mil</span>
        </p>
      </div>

      <ol className="mt-4 space-y-1 text-base font-medium sm:text-[17px]" aria-label="Resväg">
        <li className="flex items-center gap-2.5">
          <span className="size-2.5 shrink-0 rounded-full border-2 border-fg-subtle" aria-hidden />
          <span className="min-w-0 break-words">
            <span className="sr-only">Från </span>
            {place(trip, full, "start")}
          </span>
        </li>
        <li aria-hidden className="pl-[1px] text-fg-subtle">
          <ArrowDown className="size-3.5" />
        </li>
        <li className="flex items-center gap-2.5">
          <span className="size-2.5 shrink-0 rounded-full bg-fg" aria-hidden />
          <span className="min-w-0 break-words">
            <span className="sr-only">Till </span>
            {place(trip, full, "end")}
          </span>
        </li>
      </ol>

      {showCategory && (trip.category === "work" || trip.category === "private" || purpose) ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-3 text-sm">
          {trip.category === "work" || trip.category === "private" ? (
            <>
              <span className="text-fg-muted">{proposal ? "Förslag" : "Nu"}</span>
              <CategoryPill category={trip.category} preliminary={proposal} size="sm" />
            </>
          ) : null}
          {purpose ? (
            <span className="min-w-0 basis-full text-fg-muted">
              Syfte{trip.purpose_confirmed ? "" : " (förslag)"}: <span className="text-fg">{purpose}</span>
            </span>
          ) : null}
        </div>
      ) : null}
      {full ? <CollectionGapNote trip={full} compact className="mt-4 p-3" /> : null}
    </div>
  );
}

export function QuickTripCardSkeleton() {
  return (
    <div className="rounded-xl border border-border bg-surface p-5 shadow-sm" role="status" aria-label="Laddar resan">
      <div className="flex justify-between">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-6 w-16" />
      </div>
      <Skeleton className="mt-5 h-5 w-3/4" />
      <Skeleton className="mt-3 h-5 w-2/3" />
    </div>
  );
}
