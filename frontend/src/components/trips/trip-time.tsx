"use client";

import { Tooltip } from "@/components/ui/tooltip";
import { formatDateTime, formatTime } from "@/lib/format";
import { label, TIME_QUALITY_LABELS } from "@/lib/labels";
import type { Trip } from "@/lib/types";
import { QualityValue } from "./quality-value";

/** Starttidens gränser i listsvaret (nyare backend; kan saknas – då hänvisar listan till detaljerna). */
type TripBounds = Trip & { start_earliest?: string | null; start_latest?: string | null };

/** Props för starttiden i körjournalens lista och kort. */
export function listStartBounds(t: Trip): { earliest?: string | null; latest?: string | null; noBoundsText: string } {
  const b = t as TripBounds;
  const known = "start_earliest" in b || "start_latest" in b;
  return {
    earliest: b.start_earliest,
    latest: b.start_latest,
    noBoundsText: known ? "Inga kända gränser." : "Se resans detaljer för möjligt intervall.",
  };
}

/**
 * Klockslag med kvalitetsmärkning. En okänd tid visas som "okänd" (aldrig ett
 * påhittat klockslag) med kända gränser i tooltip.
 */
export function TripTime({
  at,
  quality,
  earliest,
  latest,
  withDate = false,
  noBoundsText = "Inga kända gränser.",
}: {
  at: string | null | undefined;
  quality: string | null | undefined;
  earliest?: string | null;
  latest?: string | null;
  withDate?: boolean;
  /** Text när varken tidigaste eller senaste tid är känd. */
  noBoundsText?: string;
}) {
  const fmt = withDate ? formatDateTime : formatTime;
  if (!at || quality === "unknown" || quality === "bounded") {
    const bounds =
      earliest || latest
        ? `Någon gång ${earliest ? `efter ${formatDateTime(earliest)}` : ""}${earliest && latest ? " och " : ""}${latest ? `före ${formatDateTime(latest)}` : ""}.`
        : noBoundsText;
    return (
      <Tooltip content={<><strong className="font-semibold">Tiden är okänd</strong><br />{bounds}</>}>
        <span tabIndex={0} className="cursor-help text-fg-subtle italic outline-none focus-visible:outline-2 focus-visible:outline-ring">
          okänd
        </span>
      </Tooltip>
    );
  }
  return (
    <QualityValue
      value={fmt(at)}
      quality={quality === "estimated" ? "estimated" : "actual"}
      explanation={`${label(TIME_QUALITY_LABELS, quality)} – ungefärligt klockslag inom ett snävt intervall.`}
    />
  );
}
