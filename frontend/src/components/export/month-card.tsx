"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, CalendarCheck, Clock, Download, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDateTime, plural } from "@/lib/format";
import type { ExportJob, MonthBlocker, MonthStatus } from "@/lib/exports-types";
import { label, MONTH_STATE_LABELS } from "@/lib/labels";
import { exportDownloadUrl, useExports, useMonthClose, useRunMonthClose } from "@/lib/queries/exports";
import { useSettings } from "@/lib/queries/settings";
import { cn } from "@/lib/utils";
import { monthLabel } from "./export-period";

const STATE_TONE: Record<string, "neutral" | "warn" | "ok" | "accent"> = {
  open: "neutral",
  waiting: "warn",
  exported: "ok",
  stale: "warn",
  empty: "neutral",
};

/** Upplysningar som inte visas som egna rader (de syns i listan över antagna och automatiska). */
const HIDDEN_NOTES = new Set(["assumed", "auto_approved"]);

function BlockerLine({ b }: { b: MonthBlocker }) {
  return (
    <li className="flex items-start gap-2">
      <Clock className="mt-0.5 size-3.5 shrink-0 text-warn-ink" aria-hidden />
      <span>
        {b.text}
        {b.web_only ? <span className="text-fg-muted"> – öppna webben för att svara</span> : null}
        {b.trip_ids.length === 1 ? (
          <>
            {" "}
            <Link href={`/journal/${b.trip_ids[0]}`} className="text-accent-ink hover:underline focus-visible:outline-2 focus-visible:outline-ring">
              Öppna resan
            </Link>
          </>
        ) : b.code === "open_questions" || b.code === "unapproved_work" ? (
          <>
            {" "}
            <Link href="/review" className="text-accent-ink hover:underline focus-visible:outline-2 focus-visible:outline-ring">
              Granska
            </Link>
          </>
        ) : null}
      </span>
    </li>
  );
}

function NoteLine({ n }: { n: MonthBlocker }) {
  const missingStart = n.code === "missing_start_time";
  return (
    <li className={cn("text-fg-muted", missingStart && "text-fg")} data-testid={`month-note-${n.code}`}>
      {n.text}
      {missingStart ? (
        <span className="text-fg-muted">
          {" "}
          – stoppar inte underlaget, där skrivs bara sluttiden (t.ex. &quot;–08:52&quot;). Ange starttiden på resan om du vet den.
          {n.trip_ids.length === 1 ? (
            <>
              {" "}
              <Link href={`/journal/${n.trip_ids[0]}`} className="text-accent-ink hover:underline focus-visible:outline-2 focus-visible:outline-ring">
                Öppna resan
              </Link>
            </>
          ) : null}
        </span>
      ) : null}
    </li>
  );
}

function stateText(s: MonthStatus, job: ExportJob | undefined): React.ReactNode {
  switch (s.state) {
    case "exported":
      return job
        ? `Klart. Underlaget skapades ${job.origin === "month_close" ? "automatiskt " : ""}${formatDateTime(job.created_at)}.`
        : "Klart. Underlaget är skapat.";
    case "stale":
      return "Något har ändrats sedan underlaget skapades. En ny fil skapas vid nästa kontroll, och du får en notis.";
    case "empty":
      return "Inga tjänsteresor – ingen fil behövs.";
    default: {
      if (s.blockers.some((b) => b.code === "month_not_over")) return "Månaden pågår. Underlaget skapas av sig självt när den är slut.";
      const waitingFor = s.blockers.some((b) => b.code !== "month_not_over");
      if (waitingFor) return "Underlaget skapas av sig självt när det här är klart:";
      return s.state === "waiting" ? "Väntar på nästa kontroll." : "Månaden är inte kontrollerad än.";
    }
  }
}

function MonthRow({ status, current, jobs }: { status: MonthStatus; current: boolean; jobs: ExportJob[] }) {
  const run = useRunMonthClose();
  const job = status.export_id ? jobs.find((j) => j.id === status.export_id) : undefined;
  const blockers = status.blockers.filter((b) => b.code !== "month_not_over");
  const notes = status.notes.filter((n) => !HIDDEN_NOTES.has(n.code) && n.text);
  const assumed = status.review_rows.filter((r) => r.approval_source === "assumed").length;
  const automatic = status.review_rows.length - assumed;
  const title = monthLabel(status.month);

  const check = () =>
    run.mutate(status.month, {
      onSuccess: (s) => toast.success(`${title}: ${label(MONTH_STATE_LABELS, s.state).toLowerCase()}.`),
      onError: (e) => toast.error(errorMessage(e)),
    });

  return (
    <li className="px-5 py-4" data-testid="month-row" data-month={status.month} data-state={status.state}>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
          {title}
          <Badge tone={STATE_TONE[status.state] ?? "neutral"} size="sm">
            {label(MONTH_STATE_LABELS, status.state)}
          </Badge>
        </h3>
        <div className="flex flex-wrap items-center gap-1.5">
          {job && job.status === "done" && !job.superseded_by_id ? (
            <Button asChild size="sm" variant="secondary">
              <a href={exportDownloadUrl(job.id)} download={job.filename ?? undefined}>
                <Download aria-hidden /> Hämta underlaget
              </a>
            </Button>
          ) : null}
          {!current ? (
            <Button size="sm" variant="ghost" onClick={check} loading={run.isPending}>
              {run.isPending ? null : <RefreshCw aria-hidden />} Kontrollera nu
            </Button>
          ) : null}
        </div>
      </div>
      <p className="mt-1.5 text-[13px] text-fg-muted">{stateText(status, job)}</p>
      {job?.origin === "month_close" && typeof job.link_downloads === "number" && job.link_downloads > 0 ? (
        <p className="mt-0.5 text-xs text-fg-muted">Hämtad {plural(job.link_downloads, "gång", "gånger")} via länken i notisen.</p>
      ) : null}
      {blockers.length && status.state !== "exported" && status.state !== "empty" ? (
        <ul className="mt-2 space-y-1 text-[13px] text-fg">
          {blockers.map((b) => (
            <BlockerLine key={b.code} b={b} />
          ))}
        </ul>
      ) : null}
      {notes.length ? (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[13px]">
          {notes.map((n) => (
            <NoteLine key={n.code} n={n} />
          ))}
        </ul>
      ) : null}
      {status.review_rows.length ? (
        <p className="mt-2 text-[13px]">
          <Link
            href={`/m/month/${status.month}`}
            className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
            data-testid="month-review-link"
          >
            {assumed ? `Se antagna (${assumed})` : `Se automatiskt godkända (${automatic})`}
            <ArrowRight className="size-3.5" aria-hidden />
          </Link>
          {assumed && automatic ? <span className="text-fg-muted"> · {plural(automatic, "resa", "resor")} godkänd automatiskt</span> : null}
        </p>
      ) : null}
    </li>
  );
}

/**
 * Månadskortet på exportsidan (byggkontraktet C.6): läget för föregående och innevarande
 * månad, vad som väntar, det automatiskt skapade underlaget och månadens antagna rader.
 */
export function MonthCard() {
  const months = useMonthClose();
  const exports = useExports();
  const settings = useSettings();
  const manual = settings.data?.settings.automation?.mode === "manual";

  if (months.isError) {
    return (
      <Card className="mb-6">
        <ErrorState compact error={months.error} onRetry={() => months.refetch()} title="Det gick inte att hämta månadens läge" />
      </Card>
    );
  }
  if (!months.data) {
    return months.isPending && months.fetchStatus !== "idle" ? (
      <Card className="mb-6 space-y-2 p-5" role="status" aria-label="Laddar månadens läge">
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-3 w-72 max-w-full" />
      </Card>
    ) : null;
  }
  // Föregående månad först – det är den som ska lämnas in.
  const [current, previous] = months.data.months;
  const rows = [previous, current].filter((m): m is MonthStatus => !!m);
  if (!rows.length) return null;

  return (
    <Card className="mb-6" data-testid="month-card">
      <div className="flex items-start gap-3 border-b border-border px-5 py-4">
        <CalendarCheck className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
        <div className="min-w-0">
          <h2 className="text-sm font-semibold tracking-tight text-fg">Månadens underlag</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-fg-muted">
            {manual
              ? "Du godkänner resorna själv. När månaden är klar kan du ändå låta DriveLog kontrollera den och skapa underlaget."
              : "Skapas av sig självt när månaden är slut och allt är besvarat. Du får en notis med en länk."}
          </p>
        </div>
      </div>
      <ul className="divide-y divide-border">
        {rows.map((m) => (
          <MonthRow key={m.month} status={m} current={m === current} jobs={exports.data ?? []} />
        ))}
      </ul>
    </Card>
  );
}
