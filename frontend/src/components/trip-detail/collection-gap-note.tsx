"use client";

import Link from "next/link";
import { CloudOff, FileUp, Split } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatNumber, formatSpanParts } from "@/lib/format";
import type { Trip } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Visas resan efter ett uppehåll i hämtningen från Volvo? (fältet från 1.3.0 eller bara flaggan) */
export function hasCollectionGap(trip: Pick<Trip, "quality_flags" | "collection_gap"> | null | undefined): boolean {
  return !!trip && (!!trip.collection_gap || trip.quality_flags.includes("collection_gap"));
}

/** Förklaringen – samma innehåll som detection.collection_gap_text(form="long"), skriven för webben. */
export function collectionGapText(trip: Pick<Trip, "collection_gap">): string {
  const gap = trip.collection_gap;
  const advice =
    "Importera Volvos körjournal för dagen – då ersätts resan av de riktiga resorna – eller dela upp kilometrarna i tjänst och privat.";
  if (!gap) return `Hämtningen från Volvo hade ett uppehåll före resan, så den kan vara flera körningar. ${advice}`;
  const [from, to] = formatSpanParts(gap.from, gap.to);
  if (gap.odometer_km != null) {
    return `Hämtningen låg nere mellan ${from} och ${to}. Under tiden ökade mätaren med ${formatNumber(gap.odometer_km, 1)} km, så resan kan vara flera körningar. ${advice}`;
  }
  return `Hämtningen låg nere mellan ${from} och ${to}, så resan kan vara flera körningar. ${advice}`;
}

/**
 * Varning när resan kom efter ett uppehåll i hämtningen från Volvo (DEL 2, 1.3.0): när
 * hämtningen var borta och hur mycket mätaren ökade under tiden. I resedetaljen med
 * knapparna "Importera körjournal" och "Dela upp" (en delresa), i telefonvyn utan knappar.
 */
export function CollectionGapNote({
  trip,
  onSplit,
  compact,
  className,
}: {
  trip: Trip;
  /** Öppnar fördelningen tjänst/privat för resans enda delresa. Utelämnas när resan har flera delresor. */
  onSplit?: () => void;
  /** Utan knappar (telefonvyn /m/q). */
  compact?: boolean;
  className?: string;
}) {
  if (!hasCollectionGap(trip)) return null;
  return (
    <section
      aria-labelledby={`collection-gap-${trip.id}`}
      className={cn("rounded-xl border border-warn/40 bg-warn-soft p-4 text-warn-ink", className)}
      data-testid="collection-gap-note"
    >
      <div className="flex items-start gap-2.5">
        <CloudOff className="mt-0.5 size-4 shrink-0" aria-hidden />
        <div className="min-w-0">
          <h2 id={`collection-gap-${trip.id}`} className="text-sm font-semibold">
            Uppehåll i hämtningen från Volvo
          </h2>
          <p className="mt-1 text-[13px] leading-relaxed">{collectionGapText(trip)}</p>
          {!compact ? (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button asChild size="sm" variant="secondary">
                <Link href="/import">
                  <FileUp aria-hidden /> Importera körjournal
                </Link>
              </Button>
              {onSplit ? (
                <Button size="sm" variant="secondary" onClick={onSplit}>
                  <Split aria-hidden /> Dela upp
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
