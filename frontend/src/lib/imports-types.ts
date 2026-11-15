/**
 * Typer för vyn Import (förhandsgranskning, bekräftelse och tidigare importer).
 *
 * `ImportBatchOut.summary` och svaren från commit/undo/rows beskrivs i OpenAPI som
 * `dict[str, Any]`. De är handskrivna här mot:
 *  - backend/app/services/importer/service.py → _summarize(), commit(), undo()
 *  - backend/app/services/importer/parse.py   → _parse_row() (ImportRow.parsed), _raw_repr() (ImportRow.raw)
 *  - backend/app/api/routes/imports.py        → rows(), list_batches()
 *  - backend/app/models/enums.py              → ImportRowStatus, BatchStatus
 */

import type { components } from "./api-types";

type S = components["schemas"];

/** backend/app/models/enums.py → BatchStatus */
export type BatchStatusCode = "preview" | "committed" | "undone" | "failed";

/** backend/app/models/enums.py → ImportRowStatus */
export type ImportRowStatusCode = "ok" | "duplicate" | "duplicate_differs" | "error" | "imported" | "skipped";

/** ImportedFile.status (backend/app/models/domain.py) */
export type ImportFileStatusCode = "parsed" | "duplicate_file" | "error";

export interface ImportFileInfo {
  id: string;
  filename: string;
  /** T.ex. volvo_xlsx_v1_14col, volvo_xlsx_v2_12col, volvo_csv_v2_12col, volvo_xlsx_custom, unknown. */
  format: string;
  sheet: string | null;
  rows: number;
  status: ImportFileStatusCode;
  /** Varför filen inte kunde läsas (svensk text), annars null. */
  error: string | null;
  warnings: string[];
  ignored_columns: string[];
}

export interface ImportSummary {
  files: ImportFileInfo[];
  rows_total: number;
  /** Rader som blir (eller blev) nya resor. Sträcka, bensin, el och period avser dessa. */
  rows_new: number;
  rows_duplicate: number;
  rows_duplicate_differs: number;
  rows_error: number;
  distance_km: number;
  fuel_liters: number;
  energy_kwh: number;
  categories: Record<string, number>;
  period_start: string | null;
  period_end: string | null;
  /** Neutrala upplysningar om underlaget (visas som information, aldrig som varning). */
  notes: string[];
  // Tillkommer när importen har bekräftats (commit):
  imported?: number;
  auto_merged?: number;
  merge_questions?: number;
  /** Antal adresser i importen som kopplats till en plats (befintlig eller nyskapad). */
  locations_created?: number;
  reconciled_api_trips?: { superseded?: number; transferred?: number; review_items?: number; exact?: number };
}

export interface ImportBatch extends Omit<S["ImportBatchOut"], "summary" | "status"> {
  status: BatchStatusCode;
  /** Tom ({}) i teorin om sammanställningen aldrig hann skapas – läs fälten defensivt. */
  summary: Partial<ImportSummary>;
}

export interface ImportBatchList {
  items: ImportBatch[];
}

/** Originalvärde i en cell: text/tal som i filen, eller en datum-/tidscell från Excel. */
export type ImportRawValue =
  | string
  | number
  | boolean
  | null
  | { type: "datetime" | "date" | "time"; value: string }
  | { type: "timedelta"; seconds: number };

export interface ImportRowParsed {
  started_at?: string;
  ended_at?: string;
  distance_km?: number;
  odometer_start?: number | null;
  odometer_end?: number | null;
  start_address?: string | null;
  end_address?: string | null;
  original_notes?: string | null;
}

export interface ImportRow {
  id: string;
  file_id: string;
  filename: string | null;
  /** Radnummer i filen (rubrikraden är rad 1). */
  row_number: number;
  /** Originalvärdena, med filens egna kolumnrubriker som nycklar. */
  raw: Record<string, ImportRawValue>;
  parsed: ImportRowParsed;
  status: ImportRowStatusCode;
  messages: string[];
  raw_trip_id: string | null;
}

export interface ImportRowList {
  items: ImportRow[];
  total: number;
  page: number;
  page_size: number;
}

/** Svar från POST /imports/{id}/commit: sammanställningen plus utfallet. */
export interface ImportCommitResult extends Partial<ImportSummary> {
  batch_id: string;
  /** Sant om importen redan var bekräftad (t.ex. dubbelklick); fälten kommer då ur den sparade sammanställningen. */
  already_committed: boolean;
}

export interface ImportUndoResult {
  batch_id: string;
  /** Antal resor som makulerades. */
  voided: number;
  already_undone: boolean;
}
