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
        <span className={cn("text-xl leading-none font-medium tracking-tight num", tone === "accent" && "text-accent-ink", tone === "err" && "text-err-ink")}>{value}</span>
        {unit ? <span className="text-xs text-fg-muted">{unit}</span> : null}
      </dd>
      {hint ? <dd className="mt-1 text-xs text-fg-subtle">{hint}</dd> : null}
    </div>
  );
}

function FileRow({ file, restored }: { file: ImportFileInfo; restored: boolean }) {
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
            {file.status === "duplicate_file" ? (
              <>
                {" "}
                <span aria-hidden>·</span> samma fil har importerats tidigare
                {restored ? (
                  <>
                    {" "}
                    <span aria-hidden>·</span> borttagna resor kommer tillbaka
                  </>
                ) : null}
              </>
            ) : null}
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

interface MergeSettings {
  enabled?: boolean;
  by_category?: boolean;
  auto_max_minutes?: number;
  ask_max_minutes?: number;
  private_auto_max_minutes?: number;
  private_ask_max_minutes?: number;
}

/** Hinttexten under kryssrutan – följer regelverket i backend/app/services/trips/merge.py. */
function mergeHint(cfg: MergeSettings | undefined): string {
  const auto = formatNumber(cfg?.auto_max_minutes ?? 15, 0);
  const ask = formatNumber(cfg?.ask_max_minutes ?? 60, 0);
  if (cfg?.by_category !== true) {
    return `Resor med högst ${auto} minuters stopp emellan blir en resa. Vid längre stopp, upp till ${ask} minuter, får du en fråga i Granskning.`;
  }
  const pautoN = cfg.private_auto_max_minutes ?? 15;
  const paskN = cfg.private_ask_max_minutes ?? 15;
  const pauto = formatNumber(pautoN, 0);
  const pask = formatNumber(paskN, 0);
  const question =
    paskN > pautoN
      ? ` Upp till ${ask} min (tjänst) och ${pask} min (privat) får du en fråga i Granskning.`
      : ` Upp till ${ask} min (tjänst) får du en fråga i Granskning.`;
  return `Två tjänsteresor med högst ${auto} min stopp emellan blir en resa, likaså två privata med högst ${pauto} min.${question} Tjänst och privat slås aldrig ihop.`;
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
  // Resor som ägaren har tagit bort och som läggs in igen (räknas bland rows_new).
  const restored = Math.min(s.rows_restored ?? 0, rowsNew);

  const settings = useSettings();
  const mergeCfg = settings.data?.settings.merge as MergeSettings | undefined;
  const mergeAvailable = mergeCfg?.enabled !== false;
  const [merge, setMerge] = React.useState(true);

  const allRestored = restored > 0 && restored === rowsNew;
  const title =
    allRestored
      ? restored === 1
        ? "1 borttagen resa kommer tillbaka"
        : `${formatNumber(restored, 0)} borttagna resor kommer tillbaka`
      : rowsNew > 0
      ? `${plural(rowsNew, "ny resa", "nya resor")} att importera`
      : rowsTotal > 0
        ? "Inget nytt att importera"
        : "Filerna innehåller inga resor";
  const lead =
    allRestored
      ? `Du har tagit bort ${restored === 1 ? "resan" : "de här resorna"} tidigare. När du bekräftar läggs ${restored === 1 ? "den" : "de"} in igen som ${restored === 1 ? "den står" : "de står"} i filen – ändringar du gjorde innan du tog bort ${restored === 1 ? "den" : "dem"} kommer inte tillbaka.`
      : rowsNew > 0
      ? `Inget har sparats ännu. Kontrollera att det ser rätt ut och bekräfta sedan.${
          restored > 0 ? ` Varav ${formatNumber(restored, 0)} som du har tagit bort tidigare – de kommer tillbaka som de står i filen.` : ""
        }`
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
            <dl className="mt-5 grid grid-cols-3 gap-x-4 gap-y-5 rounded-lg border border-border bg-surface-2/40 p-4 sm:grid-cols-4 lg:grid-cols-8">
              <Metric label="Nya resor" value={formatNumber(rowsNew, 0)} tone={rowsNew > 0 ? "accent" : undefined} />
              {restored > 0 ? <Metric label="Kommer tillbaka" value={formatNumber(restored, 0)} hint="borttagna tidigare" /> : null}
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
              <FileRow key={f.id} file={f} restored={restored > 0} />
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
                  {mergeHint(mergeCfg)}
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
