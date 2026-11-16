"use client";

import * as React from "react";
import { ChevronDown, Download, FileSpreadsheet, LinkIcon, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatKm, plural } from "@/lib/format";
import type { ExportJob } from "@/lib/exports-types";
import { exportDownloadUrl, useDeleteExport, useExports, useRevokeLinks } from "@/lib/queries/exports";

const COLLAPSED_COUNT = 4;

function jobTitle(job: ExportJob): string {
  return job.summary.period_label ?? job.params.label ?? job.summary.label ?? "Körjournal";
}

function jobMeta(job: ExportJob): string {
  const parts = [job.origin === "month_close" ? `Automatiskt skapad ${formatDateTime(job.created_at)}` : `Skapad ${formatDateTime(job.created_at)}`];
  if (typeof job.summary.trips === "number") parts.push(plural(job.summary.trips, "resa", "resor"));
  if (typeof job.summary.km === "number") parts.push(formatKm(job.summary.km));
  if (job.params.layout === "detailed") parts.push("detaljerad");
  if (job.link_downloads) parts.push(`hämtad ${plural(job.link_downloads, "gång", "gånger")} via länk`);
  if (job.superseded_by_id) parts.push(job.superseded_by_id === job.id ? "återkallad, månaden har inga tjänsteresor" : "ersatt av en nyare fil");
  return parts.join(" · ");
}

/** Tidigare skapade filer – diskret längst ned, hopfälld när listan är lång. */
export function PreviousFiles() {
  const list = useExports();
  const del = useDeleteExport();
  const revoke = useRevokeLinks();
  const [revokeOpen, setRevokeOpen] = React.useState(false);
  const [expanded, setExpanded] = React.useState(false);
  const [confirm, setConfirm] = React.useState<ExportJob | null>(null);
  const jobs = list.data ?? [];

  if (list.isError) {
    return (
      <Section>
        <ErrorState compact error={list.error} onRetry={() => list.refetch()} title="Det gick inte att hämta tidigare filer" />
      </Section>
    );
  }
  if (list.isPending) {
    return (
      <Section>
        <div className="space-y-2" role="status" aria-label="Laddar tidigare filer">
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
        </div>
      </Section>
    );
  }
  if (!jobs.length) return null;

  const shown = expanded ? jobs : jobs.slice(0, COLLAPSED_COUNT);
  const hidden = jobs.length - shown.length;

  const closeLinks = () =>
    revoke.mutate(undefined, {
      onSuccess: () => {
        toast.success("Alla nedladdningslänkar är stängda.", { description: "Länkar i tidigare notiser fungerar inte längre." });
        setRevokeOpen(false);
      },
      onError: (e) => toast.error(errorMessage(e)),
    });

  const remove = (job: ExportJob) => {
    del.mutate(job.id, {
      onSuccess: () => {
        toast.success("Filen är raderad.", { description: job.filename ?? undefined });
        setConfirm(null);
      },
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  return (
    <Section count={jobs.length}>
      <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface shadow-sm">
        {shown.map((job) => {
          const done = job.status === "done";
          return (
            <li key={job.id} className="flex items-center gap-3 px-4 py-3">
              <FileSpreadsheet className="size-4 shrink-0 text-fg-subtle" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-fg">
                  {jobTitle(job)}
                  {job.summary.is_demo ? <span className="ml-2 text-xs font-normal text-warn-ink">Demodata</span> : null}
                </p>
                <p className="truncate text-xs text-fg-muted">{done ? jobMeta(job) : `Misslyckades · ${formatDateTime(job.created_at)}`}</p>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {done ? (
                  <Button asChild variant="ghost" size="sm">
                    <a href={exportDownloadUrl(job.id)} download={job.filename ?? undefined} aria-label={`Hämta ${job.filename ?? jobTitle(job)}`}>
                      <Download aria-hidden /> <span className="hidden sm:inline">Hämta</span>
                    </a>
                  </Button>
                ) : null}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="text-fg-subtle hover:text-err-ink"
                  onClick={() => setConfirm(job)}
                  aria-label={`Radera ${job.filename ?? jobTitle(job)}`}
                >
                  <Trash2 aria-hidden />
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        {hidden > 0 || expanded ? (
        <Button variant="link" size="sm" className="text-fg-muted hover:text-fg" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
          <ChevronDown className={expanded ? "rotate-180 transition-transform" : "transition-transform"} aria-hidden />
          {expanded ? "Visa färre" : `Visa alla ${jobs.length}`}
        </Button>
        ) : (
          <span />
        )}
        <Button variant="ghost" size="sm" className="text-fg-muted hover:text-fg" onClick={() => setRevokeOpen(true)} data-testid="revoke-links">
          <LinkIcon aria-hidden /> Stäng alla nedladdningslänkar
        </Button>
      </div>

      <Dialog open={revokeOpen} onOpenChange={(o) => !revoke.isPending && setRevokeOpen(o)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Stäng alla nedladdningslänkar?</DialogTitle>
            <DialogDescription>
              Länkarna i notiserna går att öppna utan inloggning i 7 dagar. Stänger du dem fungerar ingen tidigare länk längre. Filerna finns kvar här och
              nästa notis får en ny länk.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setRevokeOpen(false)} disabled={revoke.isPending}>
              Avbryt
            </Button>
            <Button variant="danger" onClick={closeLinks} loading={revoke.isPending} data-testid="revoke-links-confirm">
              Stäng länkarna
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!confirm} onOpenChange={(o) => !o && !del.isPending && setConfirm(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Radera filen?</DialogTitle>
            <DialogDescription>
              {confirm?.filename ?? "Filen"} tas bort från servern. Resorna påverkas inte och du kan skapa en ny fil när du vill.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={del.isPending}>
              Avbryt
            </Button>
            <Button variant="danger" onClick={() => confirm && remove(confirm)} loading={del.isPending}>
              Radera
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Section>
  );
}

function Section({ children, count }: { children: React.ReactNode; count?: number }) {
  return (
    <section aria-labelledby="export-history-title" className="mt-12">
      <h2 id="export-history-title" className="mb-3 flex items-baseline gap-2 text-sm font-semibold tracking-tight text-fg">
        Tidigare filer
        {count ? <span className="text-xs font-normal text-fg-muted">{count}</span> : null}
      </h2>
      {children}
    </section>
  );
}
