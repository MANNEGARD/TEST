"use client";

import * as React from "react";
import { Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Pagination } from "@/components/ui/pagination";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { formatNumber, plural } from "@/lib/format";
import type { ImportRow } from "@/lib/imports-types";
import { IMPORT_ROW_STATUS_LABELS, label } from "@/lib/labels";
import { useImportRows, type ImportRowFilter } from "@/lib/queries/imports";
import { cn } from "@/lib/utils";
import { IMPORT_ROW_STATUS_TONE, rawValueLabel } from "./import-labels";

const PAGE_SIZE = 50;

export interface RowCounts {
  all: number;
  new: number;
  duplicates: number;
  errors: number;
}

const FILTERS: { value: ImportRowFilter; label: string; count: (c: RowCounts) => number }[] = [
  { value: "all", label: "Alla", count: (c) => c.all },
  { value: "new", label: "Nya", count: (c) => c.new },
  { value: "duplicates", label: "Dubbletter", count: (c) => c.duplicates },
  { value: "errors", label: "Fel", count: (c) => c.errors },
];

const EMPTY_TEXT: Record<ImportRowFilter, string> = {
  all: "Filerna innehåller inga rader.",
  new: "Inga rader blir nya resor.",
  duplicates: "Inga rader finns redan i körjournalen.",
  errors: "Alla rader kunde läsas.",
};

/** Kolumnrubrikerna i den ordning de står i filen (unionen över sidans rader). */
function rawColumns(rows: ImportRow[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    for (const k of Object.keys(r.raw)) {
      if (!seen.has(k)) {
        seen.add(k);
        out.push(k);
      }
    }
  }
  return out;
}

function StatusCell({ row }: { row: ImportRow }) {
  return (
    <Badge tone={IMPORT_ROW_STATUS_TONE[row.status] ?? "neutral"} size="sm">
      {label(IMPORT_ROW_STATUS_LABELS, row.status)}
    </Badge>
  );
}

function Messages({ row, className }: { row: ImportRow; className?: string }) {
  if (!row.messages.length) return null;
  return (
    <ul className={cn("space-y-0.5 text-xs", row.status === "error" ? "text-err-ink" : "text-fg-muted", className)}>
      {row.messages.map((m) => (
        <li key={m}>{m}</li>
      ))}
    </ul>
  );
}

function RowPosition({ row, multipleFiles }: { row: ImportRow; multipleFiles: boolean }) {
  return (
    <span className="whitespace-nowrap">
      <span className="num">Rad {row.row_number}</span>
      {multipleFiles && row.filename ? <span className="block max-w-40 truncate text-[11px] text-fg-subtle" title={row.filename}>{row.filename}</span> : null}
    </span>
  );
}

function RowsBody({ batchId, filter, counts, multipleFiles }: { batchId: string; filter: ImportRowFilter; counts: RowCounts; multipleFiles: boolean }) {
  const [page, setPage] = React.useState(1);
  const q = useImportRows(batchId, filter, page, PAGE_SIZE);

  if (q.isError) return <ErrorState compact error={q.error} onRetry={() => void q.refetch()} title="Raderna kunde inte hämtas" />;
  if (!q.data) return <SkeletonBlock rows={6} className="py-2" />;
  const rows = q.data.items;
  if (!rows.length) return <EmptyState compact icon={<Info />} title="Inga rader här" description={EMPTY_TEXT[filter]} />;
  const columns = rawColumns(rows);

  return (
    <div className={cn("space-y-3 transition-opacity", q.isPlaceholderData && "opacity-60")} aria-busy={q.isFetching || undefined}>
      {/* Mobil: ett kort per rad. */}
      <ul className="space-y-2 md:hidden">
        {rows.map((r) => (
          <li key={r.id} className="rounded-lg border border-border p-3">
            <div className="flex items-start justify-between gap-2 text-xs text-fg-muted">
              <RowPosition row={r} multipleFiles={multipleFiles} />
              <StatusCell row={r} />
            </div>
            <Messages row={r} className="mt-2" />
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
              {Object.entries(r.raw).map(([k, v]) => (
                <React.Fragment key={k}>
                  <dt className="text-fg-subtle">{k}</dt>
                  <dd className="min-w-0 break-words text-fg">{rawValueLabel(v)}</dd>
                </React.Fragment>
              ))}
            </dl>
          </li>
        ))}
      </ul>

      {/* Dator: originalkolumnerna som de står i filen. */}
      <Table wrapperClassName="hidden rounded-lg border border-border md:block" className="text-[13px]">
        <caption className="sr-only">Rader i filerna med originalvärden och meddelanden</caption>
        <THead>
          <TR className="hover:bg-transparent">
            <TH scope="col">Rad</TH>
            <TH scope="col">Status</TH>
            <TH scope="col" className="min-w-56">
              Meddelande
            </TH>
            {columns.map((c) => (
              <TH key={c} scope="col">
                {c}
              </TH>
            ))}
          </TR>
        </THead>
        <TBody>
          {rows.map((r) => (
            <TR key={r.id} className="align-top">
              <TD className="align-top text-fg-muted">
                <RowPosition row={r} multipleFiles={multipleFiles} />
              </TD>
              <TD className="align-top">
                <StatusCell row={r} />
              </TD>
              <TD className="align-top">
                {r.messages.length ? <Messages row={r} /> : <span className="text-fg-subtle">–</span>}
              </TD>
              {columns.map((c) => {
                const text = rawValueLabel(r.raw[c]);
                return (
                  <TD key={c} className="max-w-56 align-top whitespace-nowrap">
                    <span className="block truncate" title={text}>
                      {text}
                    </span>
                  </TD>
                );
              })}
            </TR>
          ))}
        </TBody>
      </Table>

      {q.data.total > PAGE_SIZE ? <Pagination page={page} pageSize={PAGE_SIZE} total={q.data.total} onPage={setPage} /> : null}
      <span className="sr-only" aria-live="polite">
        {plural(FILTERS.find((f) => f.value === filter)?.count(counts) ?? q.data.total, "rad", "rader")} visas
      </span>
    </div>
  );
}

/**
 * Rader i importen med filens egna kolumner och meddelanden per rad. Hopfälld från
 * början – raderna hämtas först när sektionen öppnas.
 */
export function RowTable({ batchId, counts, multipleFiles }: { batchId: string; counts: RowCounts; multipleFiles: boolean }) {
  const [open, setOpen] = React.useState(false);
  const [filter, setFilter] = React.useState<ImportRowFilter>("all");

  return (
    <CollapsibleSection
      title="Rader i filerna"
      meta={plural(counts.all, "rad", "rader")}
      onToggle={setOpen}
      className="[&>div]:px-3 sm:[&>div]:px-5"
    >
      {open ? (
        <div className="space-y-3">
          <div className="scrollbar-thin -mx-1 overflow-x-auto px-1 pb-0.5">
            <SegmentedControl
              ariaLabel="Visa rader"
              value={filter}
              onChange={setFilter}
              options={FILTERS.map((f) => ({
                value: f.value,
                label: (
                  <>
                    {f.label}
                    <span className="num text-[11px] text-fg-subtle">{formatNumber(f.count(counts), 0)}</span>
                  </>
                ),
              }))}
            />
          </div>
          <RowsBody key={filter} batchId={batchId} filter={filter} counts={counts} multipleFiles={multipleFiles} />
        </div>
      ) : null}
    </CollapsibleSection>
  );
}
