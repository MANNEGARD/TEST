/**
 * Exportsidans period. Speglas i URL:en så att sidan går att länka och ladda om:
 *   ?manad=2026-06              en kalendermånad
 *   ?fran=2026-06-01&till=…     annan period
 * Utan parametrar: föregående kalendermånad (i användarens tidszon).
 */

import { localDateISO, monthName } from "@/lib/format";
import type { ExportRange } from "@/lib/exports-types";

export type ExportPeriod = { kind: "month"; month: string } | { kind: "custom"; from: string; to: string };

const MONTH_RE = /^(\d{4})-(0[1-9]|1[0-2])$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function isValidDate(iso: string): boolean {
  if (!DATE_RE.test(iso)) return false;
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** "2026-11" för innevarande månad. */
export function currentMonth(today: string = localDateISO()): string {
  return today.slice(0, 7);
}

export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const idx = y * 12 + (m - 1) + delta;
  const ny = Math.floor(idx / 12);
  const nm = (idx % 12) + 1;
  return `${ny}-${String(nm).padStart(2, "0")}`;
}

export function defaultMonth(today?: string): string {
  return shiftMonth(currentMonth(today), -1);
}

export function monthRange(month: string): ExportRange {
  const [y, m] = month.split("-").map(Number);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return { from: `${month}-01`, to: `${month}-${String(last).padStart(2, "0")}` };
}

/** "Juni 2026" */
export function monthLabel(month: string): string {
  const [y, m] = month.split("-").map(Number);
  const name = monthName(m);
  return `${name.charAt(0).toUpperCase()}${name.slice(1)} ${y}`;
}

export function parsePeriod(params: URLSearchParams, today?: string): ExportPeriod {
  const max = currentMonth(today);
  const manad = params.get("manad");
  if (manad && MONTH_RE.test(manad)) return { kind: "month", month: manad > max ? max : manad };
  const from = params.get("fran");
  const to = params.get("till");
  if (from && to && isValidDate(from) && isValidDate(to) && from <= to) return { kind: "custom", from, to };
  return { kind: "month", month: defaultMonth(today) };
}

export function periodRange(p: ExportPeriod): ExportRange {
  return p.kind === "month" ? monthRange(p.month) : { from: p.from, to: p.to };
}

export function periodSearch(p: ExportPeriod): string {
  return p.kind === "month" ? `?manad=${p.month}` : `?fran=${p.from}&till=${p.to}`;
}

export function isValidRange(from: string, to: string): boolean {
  return isValidDate(from) && isValidDate(to) && from <= to;
}

/** Periodnamnet mitt i en mening: "juni 2026" (datumintervall lämnas orörda). */
export function inSentence(label: string): string {
  return /^[A-ZÅÄÖ][a-zåäö]+ \d{4}$/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label;
}

/**
 * "Exportera valda" i körjournalen: id:na sparas i sessionStorage under den här nyckeln och
 * exportsidan öppnas med ?selected=1 (urvalsläge i stället för månad/period).
 */
export const EXPORT_SELECTION_KEY = "drivelog.export.selected";
export const SELECTION_PARAM = "selected";
