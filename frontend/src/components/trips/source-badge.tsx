"use client";

import { FileSpreadsheet, FlaskConical, Hand, Radio, SquareDashed } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { label, TRIP_SOURCE_LABELS, TRIP_SOURCE_SHORT } from "@/lib/labels";

const ICONS: Record<string, typeof FileSpreadsheet> = {
  volvo_import: FileSpreadsheet,
  volvo_api: Radio,
  manual: Hand,
  gap_fill: SquareDashed,
  demo: FlaskConical,
};

const EXPLAIN: Record<string, string> = {
  volvo_import: "Resan kommer från en importerad körjournal (Volvo Cars-appen). Tider, sträcka och mätarställning är uppmätta av bilen.",
  volvo_api: "Resan är rekonstruerad ur fordonsrapporter (position och mätarställning). Tider kan vara ungefärliga.",
  manual: "Resan är manuellt inmatad.",
  gap_fill: "Sammanfattning av en lucka i mätarställningen som du har intygat.",
  demo: "Påhittad demonstrationsdata.",
};

/** Datakälla för en resa. `short` ger kort etikett (tabeller), full etikett i tooltip. */
export function SourceBadge({ source, short = false }: { source: string; short?: boolean }) {
  const Icon = ICONS[source] ?? FileSpreadsheet;
  const full = label(TRIP_SOURCE_LABELS, source);
  return (
    <Tooltip content={<><strong className="font-semibold">{full}</strong><br />{EXPLAIN[source] ?? ""}</>}>
      <Badge tone={source === "demo" ? "warn" : "outline"} tabIndex={0} className="cursor-help" aria-label={`Källa: ${full}`}>
        <Icon aria-hidden />
        {short ? label(TRIP_SOURCE_SHORT, source) : full}
      </Badge>
    </Tooltip>
  );
}
