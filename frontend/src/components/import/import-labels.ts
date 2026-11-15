/**
 * Etiketter och små visningshjälpare för importvyn.
 *
 * Radstatus och importstatus har etiketter i lib/labels.ts. Filformat och filstatus
 * saknas där och ligger därför här (se backend/app/services/importer/parse.py →
 * _detect_format och ImportedFile.status).
 */

import type { BadgeProps } from "@/components/ui/badge";
import { formatBytes, formatDate, formatDuration, formatNumber, MISSING } from "@/lib/format";
import type { ImportFileStatusCode, ImportRawValue, ImportRowStatusCode } from "@/lib/imports-types";

type Tone = NonNullable<BadgeProps["tone"]>;

/** "volvo_xlsx_v2_12col" → "Volvo-export, nyare format". */
export function importFormatLabel(format: string | null | undefined): string {
  if (!format || format === "unknown") return "Okänt format";
  const csv = format.startsWith("volvo_csv");
  if (format.endsWith("_v1_14col")) return csv ? "CSV-export, äldre format" : "Volvo-export, äldre format";
  if (format.endsWith("_v2_12col")) return csv ? "CSV-export, nyare format" : "Volvo-export, nyare format";
  if (format.endsWith("_custom")) return csv ? "CSV-fil med egna kolumner" : "Excel-fil med egna kolumner";
  return format;
}

export const IMPORT_FILE_STATUS: Record<ImportFileStatusCode, { label: string; tone: Tone }> = {
  parsed: { label: "Tolkad", tone: "ok" },
  duplicate_file: { label: "Redan importerad", tone: "neutral" },
  error: { label: "Kunde inte läsas", tone: "err" },
};

export const IMPORT_ROW_STATUS_TONE: Record<ImportRowStatusCode, Tone> = {
  ok: "accent",
  imported: "ok",
  duplicate: "neutral",
  duplicate_differs: "neutral",
  error: "err",
  skipped: "neutral",
};

/** Filstorlek i klartext – se formatBytes i lib/format.ts. */
export const fileSizeLabel = formatBytes;

/** Period "2026-03-01 – 2026-03-20" (ett datum om start och slut är samma dag). */
export function periodLabel(start: string | null | undefined, end: string | null | undefined): string {
  if (!start && !end) return MISSING;
  const a = formatDate(start ?? end);
  const b = formatDate(end ?? start);
  return a === b ? a : `${a} – ${b}`;
}

/**
 * Originalvärdet i en cell så som det stod i filen. Text visas oförändrad. Excels
 * datum- och tidsceller saknar tidszon (lokal väggklocka) och får därför inte räknas
 * om – de skrivs som yyyy-MM-dd HH:mm direkt ur cellens värde.
 */
export function rawValueLabel(value: ImportRawValue | undefined): string {
  if (value == null || value === "") return MISSING;
  if (typeof value === "number") return formatNumber(value, 3, 0);
  if (typeof value === "boolean") return value ? "Ja" : "Nej";
  if (typeof value === "string") return value;
  if (value.type === "timedelta") return formatDuration(value.seconds);
  const text = value.value.replace("T", " ");
  if (value.type === "date") return text.slice(0, 10);
  return text.replace(/^(.*\d{2}:\d{2}):00(\.0+)?$/, "$1");
}
