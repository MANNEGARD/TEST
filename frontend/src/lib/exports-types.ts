/**
 * Handskrivna typer för export-API:t (svaren beskrivs som dict[str, Any] i backend).
 * Källa: backend/app/api/routes/exports.py, backend/app/services/export/{service,simple,checks}.py.
 * Månadsskiftet (GET /month-close) har ett Pydantic-schema och hämtas ur api-types.ts.
 */

import type { components } from "./api-types";

/** En redovisad tjänsteresa – exakt raden i den enkla körjournalen (simple.SimpleRow.as_dict). */
export interface ExportRow {
  trip_id: string;
  /** yyyy-MM-dd (lokalt datum) eller null */
  date: string | null;
  /** "08:37–10:01", "–09:04", "" när tiden är okänd */
  time: string;
  from: string;
  /** Slutdestination, ev. följd av radbrytning och "via a; b" för sammanslagna resor. */
  to: string;
  purpose: string;
  odometer_start: number | null;
  odometer_end: number | null;
  km: number;
}

export interface ExportTotals {
  trips: number;
  km: number;
  mil: number;
  rate_per_mil: number;
  show_amount: boolean;
  /** null när beloppet inte visas (show_amount falskt eller ingen ersättningsnivå) */
  amount: number | null;
}

export interface ExcludedGroup {
  count: number;
  km: number;
  trip_ids?: string[];
}

export interface ExportExcluded {
  pending_approval: ExcludedGroup;
  undecided: ExcludedGroup;
}

export interface ExportHeader {
  title: string;
  period: string;
  driver: string;
  employer: string | null;
  vehicle: string;
  registration_number: string | null;
}

/** GET /exports/preview */
export interface ExportPreview {
  vehicle_id: string;
  is_demo: boolean;
  layout: "simple";
  scope: "business_only" | "complete";
  period: { start: string | null; end: string | null; timezone: string; label: string };
  period_label: string;
  filename: string;
  header: ExportHeader;
  columns: string[];
  rows: ExportRow[];
  totals: ExportTotals;
  excluded: ExportExcluded;
}

export type CheckSeverity = "error" | "warning" | "info";

export interface ExportCheck {
  code: string;
  severity: CheckSeverity;
  title: string;
  message: string;
  count: number;
  trip_ids: string[];
  data: Record<string, unknown>;
}

/** GET /exports/checks */
export interface ExportChecks {
  vehicle_id: string;
  is_demo: boolean;
  scope: "business_only" | "complete";
  layout: "simple" | "detailed";
  is_complete: boolean;
  errors: number;
  warnings: number;
  infos: number;
  status_label: string;
  checks: ExportCheck[];
}

/** En skapad export (ExportOut). */
export interface ExportJob {
  id: string;
  vehicle_id: string;
  created_at: string;
  finished_at: string | null;
  params: { start?: string | null; end?: string | null; label?: string; layout?: string; trip_ids?: string[] | null };
  status: "pending" | "done" | "error" | "deleted" | string;
  filename: string | null;
  sha256: string | null;
  is_complete: boolean;
  warnings: { code: string; severity: CheckSeverity; title: string; message: string; count: number }[];
  summary: {
    layout?: string;
    trips?: number;
    km?: number;
    mil?: number;
    amount?: number | null;
    period_label?: string;
    label?: string;
    is_demo?: boolean;
    size_bytes?: number;
    [key: string]: unknown;
  };
  error: string | null;
  /** "manual" (skapad av ägaren) eller "month_close" (skapad automatiskt vid månadsskiftet). */
  origin?: "manual" | "month_close" | string;
  /** Satt när en nyare fil för samma månad har ersatt den här. */
  superseded_by_id?: string | null;
  /** Antal hämtningar via nedladdningslänken i notisen (inte via webben). */
  link_downloads?: number;
}

/** GET /month-close → innevarande och föregående månad (routes/month_close.py). */
export type MonthClose = components["schemas"]["MonthCloseOut"];
export type MonthStatus = components["schemas"]["MonthStatusOut"];
export type MonthBlocker = components["schemas"]["BlockerOut"];
export type MonthReviewRow = components["schemas"]["ReviewRowOut"];

export interface ExportRange {
  from: string;
  to: string;
}
