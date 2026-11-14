"use client";

import { Tooltip } from "@/components/ui/tooltip";
import { formatDateTime, formatTime } from "@/lib/format";
import { label, TIME_QUALITY_LABELS } from "@/lib/labels";
import { QualityValue } from "./quality-value";

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
}: {
  at: string | null | undefined;
  quality: string | null | undefined;
  earliest?: string | null;
  latest?: string | null;
  withDate?: boolean;
}) {
  const fmt = withDate ? formatDateTime : formatTime;
  if (!at || quality === "unknown" || quality === "bounded") {
    const bounds =
      earliest || latest
        ? `Någon gång ${earliest ? `efter ${formatDateTime(earliest)}` : ""}${earliest && latest ? " och " : ""}${latest ? `före ${formatDateTime(latest)}` : ""}.`
        : "Inga kända gränser.";
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
