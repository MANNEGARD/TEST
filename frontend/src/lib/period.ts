/**
 * Periodfilter som delas av Översikt och Körjournal.
 * Dashboard-API:t tar `period=<key>` (+ from/to för "custom") och räknar själv i
 * användarens tidszon. Körjournalen (GET /trips) tar lokala datum `from`/`to` –
 * `periodToRange()` räknar fram dem i användarens tidszon.
 */

import { localDateISO } from "./format";
import type { PeriodKey } from "./types";

export const PERIOD_OPTIONS: { key: PeriodKey; label: string }[] = [
  { key: "today", label: "Idag" },
  { key: "week", label: "Denna vecka" },
  { key: "month", label: "Denna månad" },
  { key: "prev_month", label: "Föregående månad" },
  { key: "year", label: "Detta år" },
  { key: "all", label: "Hela historiken" },
  { key: "custom", label: "Anpassat intervall" },
];

export function periodLabel(key: PeriodKey): string {
  return PERIOD_OPTIONS.find((p) => p.key === key)?.label ?? key;
}

export interface PeriodValue {
  key: PeriodKey;
  /** yyyy-MM-dd, endast för custom */
  from?: string;
  to?: string;
}

function parseISO(d: string): Date {
  const [y, m, day] = d.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, day));
}

function iso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addDays(dateISO: string, days: number): string {
  const d = parseISO(dateISO);
  d.setUTCDate(d.getUTCDate() + days);
  return iso(d);
}

/** Lokala datumgränser (inklusive) för en period, eller tomt för "all". */
export function periodToRange(p: PeriodValue, today: string = localDateISO()): { from?: string; to?: string } {
  const t = parseISO(today);
  switch (p.key) {
    case "today":
      return { from: today, to: today };
    case "week": {
      const wd = (t.getUTCDay() + 6) % 7; // måndag = 0
      const start = addDays(today, -wd);
      return { from: start, to: addDays(start, 6) };
    }
    case "month": {
      const start = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), 1));
      const end = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + 1, 0));
      return { from: iso(start), to: iso(end) };
    }
    case "prev_month": {
      const start = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() - 1, 1));
      const end = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), 0));
      return { from: iso(start), to: iso(end) };
    }
    case "year":
      return { from: `${t.getUTCFullYear()}-01-01`, to: `${t.getUTCFullYear()}-12-31` };
    case "custom":
      return { from: p.from, to: p.to };
    default:
      return {};
  }
}

/** Query-parametrar för dashboard-endpoints. */
export function periodQuery(p: PeriodValue): Record<string, string | undefined> {
  if (p.key === "custom") return { period: "custom", from: p.from, to: p.to };
  return { period: p.key };
}

/** Lämplig tidsupplösning för diagram över perioden. */
export function bucketFor(p: PeriodValue): "day" | "week" | "month" {
  if (p.key === "year" || p.key === "all") return "month";
  if (p.key === "custom" && p.from && p.to) {
    const days = (parseISO(p.to).getTime() - parseISO(p.from).getTime()) / 86400_000;
    if (days > 180) return "month";
    if (days > 45) return "week";
  }
  return "day";
}
