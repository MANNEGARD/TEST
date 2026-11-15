"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, BookOpen, ClipboardCheck, FileSpreadsheet, Info, ListChecks, Upload } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import type { ExportJob, ExportPreview } from "@/lib/exports-types";
import { startDownload, useCreateExport, useExportChecks, useExportPreview } from "@/lib/queries/exports";
import { useSettings } from "@/lib/queries/settings";
import { useVehicle } from "@/lib/vehicle";
import { AllClear, AttentionCard, attentionItems, flaggedTripIds } from "./attention-card";
import {
  EXPORT_SELECTION_KEY,
  inSentence,
  monthLabel,
  parsePeriod,
  periodRange,
  periodSearch,
  SELECTION_PARAM,
  type ExportPeriod,
} from "./export-period";
import { JournalPreview } from "./journal-preview";
import { PeriodPicker } from "./period-picker";
import { PreviousFiles } from "./previous-files";
import { SummaryPanel, SummaryPanelSkeleton } from "./summary-panel";

/**
 * Export – månadens underlag för milersättning. Användaren väljer månad, ser exakt det
 * som hamnar i filen och hämtar en Excel-fil till arbetsgivaren. Endast godkända
 * tjänsteresor redovisas; körning mellan dem är privat och nämns inte.
 */
export function ExportView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const period = parsePeriod(new URLSearchParams(params.toString()));
  const range = periodRange(period);
  const setPeriod = (p: ExportPeriod) => router.replace(`${pathname}${periodSearch(p)}`, { scroll: false });

  // Urvalsläge ("Exportera valda" i körjournalen): id:na ligger i sessionStorage.
  const selectionMode = params.get(SELECTION_PARAM) === "1";
  const storedSelection = useStoredSelection();
  const tripIds = selectionMode ? storedSelection : null;
  const backToMonth = () => router.replace(`${pathname}${periodSearch(period)}`, { scroll: false });

  const { vehicleId, isLoading: vehiclesLoading } = useVehicle();
  const preview = useExportPreview(selectionMode ? null : range, tripIds);
  const checks = useExportChecks(selectionMode ? null : range, tripIds);
  const settings = useSettings();
  const create = useCreateExport();
  const detailed = settings.data?.settings.export?.layout === "detailed";

  const rangeKey = tripIds ? `${vehicleId}|urval|${tripIds.join(",")}` : `${vehicleId}|${range.from}|${range.to}`;
  const [created, setCreated] = React.useState<{ key: string; job: ExportJob } | null>(null);

  const p = selectionMode && !tripIds?.length ? undefined : preview.data;
  const stale = preview.isPlaceholderData;
  const label = period.kind === "month" ? monthLabel(period.month) : `${range.from} – ${range.to}`;

  const onCreate = () => {
    create.mutate(tripIds ? { tripIds } : range, {
      onSuccess: (job) => {
        startDownload(job);
        setCreated({ key: rangeKey, job });
        toast.success("Excel-filen är skapad", {
          description: job.filename ? `${job.filename} laddas ned.` : "Filen laddas ned.",
          action: { label: "Hämta igen", onClick: () => startDownload(job) },
        });
      },
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  let body: React.ReactNode;
  if (selectionMode && storedSelection !== null && !storedSelection.length) {
    body = (
      <Card>
        <EmptyState
          icon={<ListChecks />}
          title="Inga resor valda"
          description="Markera resor i körjournalen och välj Exportera valda, eller gå tillbaka till månadens underlag."
          action={
            <>
              <Button variant="primary" onClick={backToMonth}>
                <ArrowLeft aria-hidden /> Till månadens underlag
              </Button>
              <Button asChild variant="secondary">
                <Link href="/journal">
                  <BookOpen aria-hidden /> Körjournal
                </Link>
              </Button>
            </>
          }
        />
      </Card>
    );
  } else if (!vehiclesLoading && !vehicleId) {
    body = (
      <Card>
        <EmptyState
          icon={<FileSpreadsheet />}
          title="Inget fordon ännu"
          description="Importera körjournalen från Volvo Cars-appen så kan du skapa underlaget här."
          action={
            <Button asChild variant="primary">
              <Link href="/import">
                <Upload aria-hidden /> Importera
              </Link>
            </Button>
          }
        />
      </Card>
    );
  } else if (preview.isError && !p) {
    body = (
      <Card>
        <ErrorState error={preview.error} onRetry={() => preview.refetch()} title="Det gick inte att hämta underlaget" />
      </Card>
    );
  } else if (!p) {
    body = <LoadingLayout />;
  } else if (!p.rows.length && !stale) {
    body = tripIds ? <NoTripsInSelection preview={p} onBack={backToMonth} /> : <NoTrips preview={p} label={label} />;
  } else {
    const items = attentionItems(p, checks.data);
    const left = tripIds ? tripIds.length - new Set(p.rows.map((r) => r.trip_id)).size : 0;
    body = (
      <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_18rem]">
        <aside className="grid gap-4 md:grid-cols-2 md:items-start xl:sticky xl:top-20 xl:col-start-2 xl:row-start-1 xl:grid-cols-1">
          <SummaryPanel
            preview={p}
            creating={create.isPending}
            disabled={stale}
            created={created?.key === rangeKey ? created.job : null}
            onCreate={onCreate}
            onDownloadAgain={startDownload}
            hideFilename={detailed}
            footer={!checks.isPending && !items.length ? <AllClear periodLabel={p.period_label} /> : null}
          />
          {checks.isPending ? null : <AttentionCard items={items} />}
        </aside>
        <div className="min-w-0 xl:col-start-1 xl:row-start-1">
          {left > 0 && !stale ? (
            <p role="status" className="mb-3 flex items-start gap-2 text-[13px] text-fg-muted">
              <Info className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
              <span>
                {left === 1 ? "1 av de valda resorna" : `${left} av de valda resorna`} tas inte med – underlaget innehåller bara godkända tjänsteresor.
              </span>
            </p>
          ) : null}
          <JournalPreview preview={p} flaggedIds={flaggedTripIds(checks.data)} busy={stale} />
        </div>
      </div>
    );
  }

  return (
    <>
      <PageHeader
        title="Export"
        description="Underlag för milersättning med månadens godkända tjänsteresor – klart att skicka till arbetsgivaren."
        actions={p?.is_demo ? <Badge tone="warn">Demodata</Badge> : undefined}
      />

      <div className="mb-6 flex flex-col gap-3">
        {selectionMode ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <h2 className="text-lg font-semibold tracking-tight text-fg">
              Urval{storedSelection?.length ? ` (${plural(storedSelection.length, "resa", "resor")})` : ""}
            </h2>
            <Button variant="ghost" size="sm" onClick={backToMonth}>
              <ArrowLeft aria-hidden /> Tillbaka till månadsläget
            </Button>
          </div>
        ) : (
          <PeriodPicker value={period} onChange={setPeriod} />
        )}
        {detailed ? (
          <p className="flex items-start gap-2 text-[13px] text-fg-muted">
            <Info className="mt-0.5 size-4 shrink-0 text-accent-ink" aria-hidden />
            <span>
              Den detaljerade arbetsboken (flera blad) är vald i inställningarna. Förhandsvisningen visar tjänsteresorna.{" "}
              <Link href="/settings?sektion=ersattning" className="rounded-sm text-accent-ink hover:underline focus-visible:outline-2 focus-visible:outline-ring">
                Ändra
              </Link>
            </span>
          </p>
        ) : null}
      </div>

      {body}

      {vehicleId ? <PreviousFiles /> : null}
    </>
  );
}

function NoTrips({ preview, label }: { preview: ExportPreview; label: string }) {
  const waiting = preview.excluded.pending_approval.count + preview.excluded.undecided.count;
  return (
    <Card>
      <EmptyState
        icon={<FileSpreadsheet />}
        title={`Inga godkända tjänsteresor i ${inSentence(preview.period_label || label)}`}
        description={
          waiting > 0
            ? `${plural(waiting, "resa väntar", "resor väntar")} på granskning. Underlaget innehåller bara godkända tjänsteresor – godkänn dem så hamnar de här.`
            : "Underlaget innehåller bara godkända tjänsteresor. Välj en annan månad, eller importera och granska månadens resor."
        }
        action={
          waiting > 0 ? (
            <Button asChild variant="primary">
              <Link href="/review">
                <ClipboardCheck aria-hidden /> Granska <ArrowRight aria-hidden />
              </Link>
            </Button>
          ) : (
            <>
              <Button asChild variant="secondary">
                <Link href="/journal">
                  <BookOpen aria-hidden /> Körjournal
                </Link>
              </Button>
              <Button asChild variant="secondary">
                <Link href="/import">
                  <Upload aria-hidden /> Importera
                </Link>
              </Button>
            </>
          )
        }
      />
    </Card>
  );
}

/** Läser urvalet som körjournalen lämnade i sessionStorage (null under serverrenderingen). */
function useStoredSelection(): string[] | null {
  const raw = React.useSyncExternalStore(
    subscribeStorage,
    () => {
      try {
        return window.sessionStorage.getItem(EXPORT_SELECTION_KEY) ?? "[]";
      } catch {
        return "[]";
      }
    },
    () => null,
  );
  return React.useMemo(() => {
    if (raw == null) return null;
    try {
      const v: unknown = JSON.parse(raw);
      return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
    } catch {
      return [];
    }
  }, [raw]);
}

function subscribeStorage(onChange: () => void) {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

function NoTripsInSelection({ preview, onBack }: { preview: ExportPreview; onBack: () => void }) {
  const waiting = preview.excluded.pending_approval.count + preview.excluded.undecided.count;
  return (
    <Card>
      <EmptyState
        icon={<FileSpreadsheet />}
        title="Inga godkända tjänsteresor i urvalet"
        description={
          waiting > 0
            ? `${plural(waiting, "resa väntar", "resor väntar")} på granskning. Underlaget innehåller bara godkända tjänsteresor – godkänn dem så kommer de med.`
            : "Underlaget innehåller bara godkända tjänsteresor, och inga av de valda resorna är det."
        }
        action={
          <>
            {waiting > 0 ? (
              <Button asChild variant="primary">
                <Link href="/review">
                  <ClipboardCheck aria-hidden /> Granska <ArrowRight aria-hidden />
                </Link>
              </Button>
            ) : null}
            <Button variant="secondary" onClick={onBack}>
              <ArrowLeft aria-hidden /> Till månadens underlag
            </Button>
          </>
        }
      />
    </Card>
  );
}

function LoadingLayout() {
  return (
    <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_18rem]" role="status" aria-label="Laddar underlaget">
      <aside className="grid gap-4 md:grid-cols-2 xl:col-start-2 xl:row-start-1 xl:grid-cols-1">
        <SummaryPanelSkeleton />
      </aside>
      <div className="min-w-0 rounded-xl border border-border bg-surface p-5 shadow-md sm:p-8 xl:col-start-1 xl:row-start-1">
        <Skeleton className="h-6 w-64 max-w-full" />
        <div className="mt-5 grid max-w-lg grid-cols-2 gap-2">
          <Skeleton className="h-4" />
          <Skeleton className="h-4" />
          <Skeleton className="h-4" />
          <Skeleton className="h-4" />
        </div>
        <div className="mt-8 space-y-3">
          {Array.from({ length: 7 }, (_, i) => (
            <Skeleton key={i} className="h-5" style={{ width: `${100 - ((i * 13) % 30)}%` }} />
          ))}
        </div>
      </div>
      <span className="sr-only">Laddar…</span>
    </div>
  );
}
