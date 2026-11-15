"use client";

import * as React from "react";
import { Check, FileSpreadsheet, FileWarning, Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { formatKm, formatKwh, formatLiters, formatNumber, MISSING, milNumber, plural } from "@/lib/format";
import type { ImportBatch, ImportFileInfo } from "@/lib/imports-types";
import { useSettings } from "@/lib/queries/settings";
import { cn } from "@/lib/utils";
import { IMPORT_FILE_STATUS, importFormatLabel, periodLabel } from "./import-labels";
import { RowTable } from "./row-table";

function Metric({
  label,
  value,
  unit,
  hint,
  tone,
  className,
}: {
  label: string;
  value: React.ReactNode;
  unit?: string;
  hint?: React.ReactNode;
  tone?: "accent" | "err";
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="mt-1 flex items-baseline gap-1">
        <span className={cn("font-mono text-xl leading-none font-medium tracking-tight num", tone === "accent" && "text-accent-ink", tone === "err" && "text-err-ink")}>{value}</span>
        {unit ? <span className="text-xs text-fg-muted">{unit}</span> : null}
      </dd>
      {hint ? <dd className="mt-1 text-xs text-fg-subtle">{hint}</dd> : null}
    </div>
  );
}

function FileRow({ file }: { file: ImportFileInfo }) {
  const status = IMPORT_FILE_STATUS[file.status] ?? IMPORT_FILE_STATUS.parsed;
  const failed = file.status === "error";
  const Icon = failed ? FileWarning : FileSpreadsheet;
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <Icon className={cn("mt-0.5 size-4 shrink-0", failed ? "text-err-ink" : "text-fg-subtle")} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium" title={file.filename}>
          {file.filename}
        </p>
        {failed ? (
          <p className="mt-0.5 text-[13px] text-err-ink">{file.error ?? "Filen kunde inte läsas."}</p>
        ) : (
          <p className="mt-0.5 text-[13px] text-fg-muted">
            {importFormatLabel(file.format)} <span aria-hidden>·</span> {plural(file.rows, "rad", "rader")}
            {file.status === "duplicate_file" ? <> <span aria-hidden>·</span> samma fil har importerats tidigare</> : null}
          </p>
        )}
        {file.warnings.map((w) => (
          <p key={w} className="mt-1 flex items-start gap-1.5 text-xs text-fg-muted">
            <Info className="mt-0.5 size-3 shrink-0 text-fg-subtle" aria-hidden />
            {w}
          </p>
        ))}
        {file.ignored_columns.length ? (
          <p className="mt-1 text-xs text-fg-subtle">Kolumner som inte används: {file.ignored_columns.join(", ")}</p>
        ) : null}
      </div>
      <Badge tone={status.tone} className="mt-0.5 shrink-0">
        {file.status === "parsed" ? <Check aria-hidden /> : null}
        {status.label}
      </Badge>
    </li>
  );
}

/**
 * Steg 2: förhandsgranskning av uppladdade filer (svaret från POST /imports/preview).
 * Överst det som avgör beslutet – antal nya resor, filerna och knappen "Bekräfta
 * import" – därunder radtabellen för den som vill kontrollera i detalj.
 */
export function PreviewStep({
  batch,
  onConfirm,
  onCancel,
  confirming,
  note,
  headingRef,
}: {
  batch: ImportBatch;
  /** `applyMergeRules` är undefined när sammanslagning är avstängd i inställningarna. */
  onConfirm: (applyMergeRules: boolean | undefined) => void;
  onCancel: () => void;
  confirming: boolean;
  note?: React.ReactNode;
  headingRef?: React.Ref<HTMLHeadingElement>;
}) {
  const s = batch.summary;
  const files = s.files ?? [];
  const rowsNew = s.rows_new ?? 0;
  const rowsTotal = s.rows_total ?? 0;
  const duplicates = (s.rows_duplicate ?? 0) + (s.rows_duplicate_differs ?? 0);
  const differs = s.rows_duplicate_differs ?? 0;
  const errors = s.rows_error ?? 0;
  const failedFiles = files.filter((f) => f.status === "error").length;

  const settings = useSettings();
  const mergeCfg = settings.data?.settings.merge as { enabled?: boolean; auto_max_minutes?: number; ask_max_minutes?: number } | undefined;
  const mergeAvailable = mergeCfg?.enabled !== false;
  const autoMinutes = mergeCfg?.auto_max_minutes ?? 15;
  const askMinutes = mergeCfg?.ask_max_minutes ?? 60;
  const [merge, setMerge] = React.useState(true);

  const title =
    rowsNew > 0
      ? `${plural(rowsNew, "ny resa", "nya resor")} att importera`
      : rowsTotal > 0
        ? "Inget nytt att importera"
        : "Filerna innehåller inga resor";
  const lead =
    rowsNew > 0
      ? "Inget har sparats ännu. Kontrollera att det ser rätt ut och bekräfta sedan."
      : rowsTotal > 0 && duplicates === rowsTotal
        ? `${rowsTotal === 1 ? "Resan" : `Alla ${formatNumber(rowsTotal, 0)} resor`} i ${files.length === 1 ? "filen" : "filerna"} finns redan i körjournalen, så det finns inget att lägga till.`
        : failedFiles === files.length && files.length > 0
          ? `${files.length === 1 ? "Filen" : "Filerna"} kunde inte läsas. Välj en XLSX- eller CSV-export från Volvo Cars-appen.`
          : "Det finns inga rader som kan bli nya resor. Se detaljerna nedan.";

  return (
    <section aria-labelledby="import-preview-title" className="space-y-4">
      <Card data-testid="import-preview">
        <div className="p-5 sm:p-6">
          <h2 id="import-preview-title" ref={headingRef} tabIndex={-1} className="text-lg font-semibold tracking-tight outline-none">
            {title}
          </h2>
          <p className="mt-1 text-sm text-fg-muted">{lead}</p>

          {rowsTotal > 0 ? (
            <dl className="mt-5 grid grid-cols-3 gap-x-4 gap-y-5 rounded-lg border border-border bg-surface-2/40 p-4 sm:grid-cols-4 lg:grid-cols-7">
              <Metric label="Nya resor" value={formatNumber(rowsNew, 0)} tone={rowsNew > 0 ? "accent" : undefined} />
              <Metric
                label="Dubbletter"
                value={formatNumber(duplicates, 0)}
                hint={duplicates > 0 ? (differs > 0 ? `hoppas över, ${formatNumber(differs, 0)} avviker` : "hoppas över") : undefined}
              />
              <Metric label="Rader med fel" value={formatNumber(errors, 0)} tone={errors > 0 ? "err" : undefined} hint={errors > 0 ? "importeras inte" : undefined} />
              {rowsNew > 0 ? (
                <>
                  <Metric
                    label="Period"
                    value={<span className="text-sm leading-5 whitespace-nowrap">{periodLabel(s.period_start, s.period_end)}</span>}
                    className="col-span-3 sm:col-span-1 lg:col-span-2"
                  />
                  <Metric label="Körsträcka" value={milNumber(s.distance_km)} unit="mil" hint={formatKm(s.distance_km)} />
                  <Metric label="Bensin" value={s.fuel_liters ? formatLiters(s.fuel_liters).replace(/ l$/, "") : MISSING} unit={s.fuel_liters ? "l" : undefined} />
                  <Metric label="El" value={s.energy_kwh ? formatKwh(s.energy_kwh).replace(/ kWh$/, "") : MISSING} unit={s.energy_kwh ? "kWh" : undefined} />
                </>
              ) : null}
            </dl>
          ) : null}

          <h3 className="mt-6 mb-2 text-[13px] font-medium text-fg-muted">{files.length === 1 ? "Fil" : `Filer (${formatNumber(files.length, 0)})`}</h3>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {files.map((f) => (
              <FileRow key={f.id} file={f} />
            ))}
          </ul>

          {rowsNew > 0 && s.notes?.length ? (
            <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-border bg-surface-2/50 px-4 py-3 text-[13px] leading-relaxed text-fg-muted">
              <Info className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
              <ul className="space-y-1">
                {s.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {rowsNew > 0 && mergeAvailable ? (
            <div className="mt-5 flex items-start gap-3">
              <Checkbox id="import-merge" checked={merge} onCheckedChange={(v) => setMerge(v === true)} className="mt-0.5" aria-describedby="import-merge-hint" />
              <div className="min-w-0">
                <label htmlFor="import-merge" className="cursor-pointer text-sm font-medium">
                  Slå samman resor med korta stopp automatiskt
                </label>
                <p id="import-merge-hint" className="mt-0.5 text-[13px] leading-relaxed text-fg-muted">
                  Resor med högst {formatNumber(autoMinutes, 0)} minuters stopp emellan blir en resa. Vid längre stopp, upp till {formatNumber(askMinutes, 0)} minuter, får du en fråga i Granskning.
                </p>
              </div>
            </div>
          ) : null}
        </div>

        <div className="flex flex-col gap-3 border-t border-border px-5 py-4 sm:flex-row sm:items-center sm:px-6">
          {rowsNew > 0 ? (
            <>
              <Button variant="primary" size="lg" loading={confirming} onClick={() => onConfirm(mergeAvailable ? merge : undefined)} data-testid="import-confirm">
                {confirming ? null : <Check aria-hidden />} Bekräfta import
              </Button>
              <Button variant="ghost" size="lg" onClick={onCancel} disabled={confirming} data-testid="import-cancel">
                Avbryt
              </Button>
            </>
          ) : (
            <Button variant="primary" size="lg" onClick={onCancel} data-testid="import-cancel">
              Välj andra filer
            </Button>
          )}
          {note ? <p className="text-[13px] text-fg-muted sm:ml-auto sm:text-right">{note}</p> : null}
        </div>
      </Card>

      {rowsTotal > 0 ? <RowTable batchId={batch.id} counts={{ all: rowsTotal, new: rowsNew, duplicates, errors }} multipleFiles={files.filter((f) => f.rows > 0).length > 1} /> : null}
    </section>
  );
}
