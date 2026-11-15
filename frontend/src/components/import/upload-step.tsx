"use client";

import * as React from "react";
import { AlertTriangle, FileSpreadsheet, FileUp, FolderOpen, Loader2, RotateCw, Smartphone } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ApiError, errorMessage } from "@/lib/api";
import { formatPercent, plural } from "@/lib/format";
import { cn } from "@/lib/utils";
import { fileSizeLabel } from "./import-labels";

/** Filtyper som backend kan läsa (parse.py). Annat avvisas per fil med förklaring. */
const ACCEPT = ".xlsx,.csv,.txt,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv";

function hasFiles(e: DragEvent): boolean {
  return Array.from(e.dataTransfer?.types ?? []).includes("Files");
}

/**
 * Förklarar ett misslyckat uppladdningsförsök. API:ts text visas alltid när den finns
 * (t.ex. 413 "Filen … är större än tillåtna 10 MB."). Kommer svaret utan text – en
 * mellanliggande proxy kan avbryta stora uppladdningar – ges en ledtråd om storleken.
 */
function describeError(error: unknown, files: File[]): { detail: string; hint: string | null; retry: boolean } {
  const total = files.reduce((sum, f) => sum + f.size, 0);
  const largest = files.reduce<File | null>((max, f) => (!max || f.size > max.size ? f : max), null);
  const sizeHint =
    largest && total > 8 * 1024 * 1024
      ? `${files.length > 1 ? `Filerna är tillsammans ${fileSizeLabel(total)}` : `Filen är ${fileSizeLabel(total)}`} – en körjournal från Volvo är sällan större än 1 MB. Kontrollera att du valt rätt fil${files.length > 1 ? "er, eller ladda upp färre åt gången" : ""}.`
      : null;
  if (error instanceof ApiError) {
    if (error.status === 413) {
      return { detail: error.code === "error" ? "Filen är för stor för att laddas upp." : error.detail, hint: error.code === "error" ? sizeHint : null, retry: false };
    }
    if (error.status === 0 || error.status >= 500) {
      return { detail: sizeHint ? "Servern avbröt uppladdningen – troligen för att den är för stor." : error.detail, hint: sizeHint, retry: !sizeHint };
    }
    return { detail: error.detail, hint: null, retry: false };
  }
  return { detail: errorMessage(error), hint: null, retry: true };
}

/**
 * Steg 1: dra-och-släpp-yta + "Välj filer". Filer kan släppas var som helst på sidan
 * medan steget visas. Under uppladdningen visas förloppet i samma yta.
 */
export function UploadStep({
  onFiles,
  uploading,
  progress,
  files,
  error,
  onRetry,
  note,
  headingRef,
}: {
  onFiles: (files: File[]) => void;
  uploading: boolean;
  /** Andel skickad, 0–1. */
  progress: number;
  /** Filerna i pågående eller senast misslyckade uppladdning. */
  files: File[];
  error: unknown;
  onRetry: () => void;
  /** Kort upplysning under ytan, t.ex. vilket fordon resorna hamnar på. */
  note?: React.ReactNode;
  headingRef?: React.Ref<HTMLHeadingElement>;
}) {
  const inputRef = React.useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = React.useState(false);

  React.useEffect(() => {
    if (uploading) return;
    let depth = 0;
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      setDragging(true);
    };
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      setDragging(false);
      const dropped = Array.from(e.dataTransfer?.files ?? []);
      if (dropped.length) onFiles(dropped);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [uploading, onFiles]);

  const sending = progress < 1;
  const problem = error && !uploading ? describeError(error, files) : null;

  return (
    <section aria-labelledby="import-upload-title">
      <div
        data-testid="import-dropzone"
        data-dragging={dragging || undefined}
        className={cn(
          "flex flex-col items-center rounded-xl border-2 border-dashed px-5 py-10 text-center transition-colors sm:px-8 sm:py-14",
          dragging ? "border-accent bg-accent-soft/60" : "border-border-strong bg-surface",
        )}
      >
        {uploading ? (
          <div className="w-full max-w-sm" role="status" aria-live="polite">
            <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-accent-soft text-accent-ink">
              <Loader2 className="size-6 animate-spin" aria-hidden />
            </div>
            <h2 id="import-upload-title" className="mt-4 text-base font-semibold">
              {sending ? `Laddar upp ${plural(files.length, "fil", "filer")} …` : "Läser filerna …"}
            </h2>
            <div
              className="mt-4 h-1.5 overflow-hidden rounded-full bg-surface-3"
              role="progressbar"
              aria-label="Uppladdning"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(progress * 100)}
            >
              <div
                className={cn("h-full rounded-full bg-accent transition-[width] duration-200", !sending && "animate-shimmer")}
                style={{ width: `${Math.max(4, Math.round(progress * 100))}%` }}
              />
            </div>
            <p className="mt-2 text-xs text-fg-muted num">
              {sending ? `${formatPercent(progress)} uppladdat` : "Uppladdningen är klar – tolkar rader och letar efter dubbletter."}
            </p>
            <ul className="mt-4 space-y-1 text-left text-[13px] text-fg-muted">
              {files.slice(0, 6).map((f, i) => (
                <li key={`${f.name}-${i}`} className="flex items-center gap-2">
                  <FileSpreadsheet className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                  <span className="truncate">{f.name}</span>
                  <span className="ml-auto shrink-0 font-mono text-xs text-fg-subtle">{fileSizeLabel(f.size)}</span>
                </li>
              ))}
              {files.length > 6 ? <li className="text-xs text-fg-subtle">… och {plural(files.length - 6, "fil", "filer")} till</li> : null}
            </ul>
          </div>
        ) : (
          <>
            {problem ? (
              <div role="alert" className="mb-7 flex w-full max-w-xl items-start gap-3 rounded-lg border border-err/25 bg-err-soft px-4 py-3 text-left text-sm">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-err-ink" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-err-ink">Uppladdningen gick inte igenom</p>
                  <p className="mt-0.5 text-fg">{problem.detail}</p>
                  {problem.hint ? <p className="mt-1 text-fg-muted">{problem.hint}</p> : null}
                </div>
                {problem.retry ? (
                  <Button size="sm" variant="secondary" onClick={onRetry} className="shrink-0">
                    <RotateCw aria-hidden /> Försök igen
                  </Button>
                ) : null}
              </div>
            ) : null}
            <div
              className={cn(
                "flex size-12 items-center justify-center rounded-full transition-colors",
                dragging ? "bg-accent text-accent-fg" : "bg-accent-soft text-accent-ink",
              )}
            >
              <FileUp className="size-6" aria-hidden />
            </div>
            <h2 id="import-upload-title" ref={headingRef} tabIndex={-1} className="mt-4 text-base font-semibold tracking-tight outline-none sm:text-lg">
              {dragging ? "Släpp filerna för att ladda upp" : "Dra och släpp körjournalen här"}
            </h2>
            <p className="mt-1 max-w-md text-sm text-fg-muted">XLSX eller CSV. Du kan välja flera filer på en gång – inget sparas förrän du har sett förhandsgranskningen.</p>
            <Button variant="primary" size="lg" className="mt-5" onClick={() => inputRef.current?.click()} data-testid="import-pick">
              <FolderOpen aria-hidden /> Välj filer
            </Button>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept={ACCEPT}
              className="sr-only"
              tabIndex={-1}
              aria-hidden
              data-testid="import-input"
              onChange={(e) => {
                const picked = Array.from(e.target.files ?? []);
                e.target.value = ""; // samma fil ska kunna väljas igen
                if (picked.length) onFiles(picked);
              }}
            />
          </>
        )}
      </div>
      <p className="mt-3 flex items-start gap-2 px-1 text-[13px] leading-relaxed text-fg-muted">
        <Smartphone className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
        <span>
          Så får du filen: öppna <span className="font-medium text-fg">Körjournal</span> i Volvo Cars-appen, välj period och exportera – skicka filen till dig själv och ladda upp den här.
          {note ? <> {note}</> : null}
        </span>
      </p>
    </section>
  );
}
