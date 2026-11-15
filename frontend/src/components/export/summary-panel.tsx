"use client";

import type * as React from "react";
import { CircleCheck, Download, FileSpreadsheet } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber, formatTime } from "@/lib/format";
import type { ExportJob, ExportPreview } from "@/lib/exports-types";
import { cn } from "@/lib/utils";
import { amountNumberText, milNumberText } from "./money";

/** De stora talen och knappen "Skapa Excel". */
export function SummaryPanel({
  preview,
  creating,
  disabled,
  created,
  onCreate,
  onDownloadAgain,
  footer,
  hideFilename,
  className,
}: {
  preview: ExportPreview;
  creating: boolean;
  disabled?: boolean;
  /** Senast skapade fil för den här perioden (kvitto). */
  created: ExportJob | null;
  onCreate: () => void;
  onDownloadAgain: (job: ExportJob) => void;
  footer?: React.ReactNode;
  /** Den detaljerade arbetsboken får ett annat filnamn än förhandsvisningens. */
  hideFilename?: boolean;
  className?: string;
}) {
  const t = preview.totals;
  return (
    <section aria-labelledby="export-summary-title" className={cn("rounded-xl border border-border bg-surface p-5 shadow-sm", className)}>
      <h2 id="export-summary-title" className="text-[13px] font-medium text-fg-muted">
        Att redovisa
      </h2>
      <p className="mt-2 flex items-baseline gap-1.5">
        <span className="font-mono text-[34px] leading-none font-medium tracking-tight text-fg tabular-nums">{milNumberText(t.mil)}</span>
        <span className="text-sm text-fg-muted">mil</span>
      </p>
      <dl className="mt-4 grid grid-cols-2 gap-3 border-t border-border pt-4">
        <div>
          <dt className="text-xs text-fg-muted">Resor</dt>
          <dd className="mt-0.5 font-mono text-lg font-medium text-fg tabular-nums">{formatNumber(t.trips, 0)}</dd>
        </div>
        {t.amount != null ? (
          <div>
            <dt className="text-xs text-fg-muted">Ersättning</dt>
            <dd className="mt-0.5 font-mono text-lg font-medium text-fg tabular-nums">
              {amountNumberText(t.amount)} <span className="font-sans text-xs font-normal text-fg-muted">kr</span>
            </dd>
          </div>
        ) : (
          <div>
            <dt className="text-xs text-fg-muted">Sträcka</dt>
            <dd className="mt-0.5 font-mono text-lg font-medium text-fg tabular-nums">
              {formatNumber(t.km, 1, 1)} <span className="font-sans text-xs font-normal text-fg-muted">km</span>
            </dd>
          </div>
        )}
      </dl>

      <Button variant="primary" size="lg" className="mt-5 w-full" onClick={onCreate} loading={creating} disabled={disabled}>
        {creating ? null : <FileSpreadsheet aria-hidden />}
        {creating ? "Skapar filen…" : "Skapa Excel"}
      </Button>
      {hideFilename ? null : (
        <p className="mt-2 truncate text-center font-mono text-[11px] text-fg-subtle" title={preview.filename}>
          {preview.filename}
        </p>
      )}

      {created ? (
        <div role="status" className="mt-4 flex items-center gap-2 rounded-lg bg-ok-soft px-3 py-2 text-[13px] text-ok-ink">
          <CircleCheck className="size-4 shrink-0" aria-hidden />
          <span className="min-w-0 flex-1">Skapad {formatTime(created.created_at)}</span>
          <Button variant="link" size="sm" className="text-ok-ink" onClick={() => onDownloadAgain(created)}>
            <Download aria-hidden /> Hämta igen
          </Button>
        </div>
      ) : null}
      {footer ? <div className="mt-5 border-t border-border pt-4">{footer}</div> : null}
    </section>
  );
}

export function SummaryPanelSkeleton({ className }: { className?: string }) {
  return (
    <div className={cn("rounded-xl border border-border bg-surface p-5 shadow-sm", className)} role="status" aria-label="Laddar">
      <Skeleton className="h-3.5 w-20" />
      <Skeleton className="mt-3 h-9 w-32" />
      <div className="mt-4 grid grid-cols-2 gap-3 border-t border-border pt-4">
        <Skeleton className="h-10" />
        <Skeleton className="h-10" />
      </div>
      <Skeleton className="mt-5 h-11 w-full" />
    </div>
  );
}
