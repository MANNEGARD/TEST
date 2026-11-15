"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, BookOpen, ClipboardCheck, FileSpreadsheet, Info, Upload } from "lucide-react";
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
import { inSentence, monthLabel, parsePeriod, periodRange, periodSearch, type ExportPeriod } from "./export-period";
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

  const { vehicleId, isLoading: vehiclesLoading } = useVehicle();
  const preview = useExportPreview(range);
  const checks = useExportChecks(range);
  const settings = useSettings();
  const create = useCreateExport();
  const detailed = settings.data?.settings.export?.layout === "detailed";

  const rangeKey = `${vehicleId}|${range.from}|${range.to}`;
  const [created, setCreated] = React.useState<{ key: string; job: ExportJob } | null>(null);

  const p = preview.data;
  const stale = preview.isPlaceholderData;
  const label = period.kind === "month" ? monthLabel(period.month) : `${range.from} – ${range.to}`;

  const onCreate = () => {
    create.mutate(range, {
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
  if (!vehiclesLoading && !vehicleId) {
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
    body = <NoTrips preview={p} label={label} />;
  } else {
    const items = attentionItems(p, checks.data);
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
            footer={!checks.isPending && !items.length ? <AllClear periodLabel={p.period_label} /> : null}
          />
          {checks.isPending ? null : <AttentionCard items={items} />}
        </aside>
        <div className="min-w-0 xl:col-start-1 xl:row-start-1">
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
        <PeriodPicker value={period} onChange={setPeriod} />
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
