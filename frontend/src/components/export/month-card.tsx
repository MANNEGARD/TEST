"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, CalendarCheck, Clock, Download, FileCheck, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatDayMonth, formatTime, monthName, plural } from "@/lib/format";
import type { ExportJob, MonthBlocker, MonthStatus } from "@/lib/exports-types";
import { RejectCalendarPurposeButton } from "@/components/trip-detail/classification-card";
import { label, MONTH_STATE_LABELS, PURPOSE_FROM_CALENDAR } from "@/lib/labels";
import { exportDownloadUrl, useExports, useMonthClose, useRunMonthClose } from "@/lib/queries/exports";
import { useSettings } from "@/lib/queries/settings";
import { useTripDetail } from "@/lib/queries/trips";
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

/**
 * "Ärende ur kalendern: {n} resor" (beslut A): resor som godkändes automatiskt och fick sitt ärende ur
 * en kalenderbokning utan fråga. Varje resa kan rättas med "Ärendet stämmer inte".
 */
function CalendarPurposeNote({ n }: { n: MonthBlocker }) {
  const [open, setOpen] = React.useState(false);
  return (
    <li className="text-fg-muted" data-testid={`month-note-${n.code}`}>
      {n.text}
      {n.trip_ids.length ? (
        <>
          {" "}
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            className="text-accent-ink hover:underline focus-visible:outline-2 focus-visible:outline-ring"
          >
            {open ? "Dölj resorna" : "Se resorna"}
          </button>
        </>
      ) : null}
      {open ? (
        <ul className="mt-1.5 space-y-1.5" aria-label="Resor med ärende ur kalendern">
          {n.trip_ids.map((id) => (
            <CalendarPurposeTrip key={id} tripId={id} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function CalendarPurposeTrip({ tripId }: { tripId: string }) {
  const detail = useTripDetail(tripId);
  const t = detail.data?.trip;
  const stillFromCalendar = !!detail.data?.purpose_history.find((p) => p.is_current)?.from_calendar && !!t?.purpose_confirmed;
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 rounded-md border border-border bg-surface px-3 py-2 text-fg">
      <span className="min-w-0">
        {t ? (
          <Link href={`/journal/${tripId}`} className="font-medium hover:underline focus-visible:outline-2 focus-visible:outline-ring">
            <span className="num mr-1.5 font-normal text-fg-muted">{formatDayMonth(t.started_at ?? t.sort_time ?? t.ended_at ?? "", false)}</span>
            {t.purpose ?? "Utan ärende"}
          </Link>
        ) : detail.isError ? (
          <Link href={`/journal/${tripId}`} className="text-accent-ink hover:underline">
            Öppna resan
          </Link>
        ) : (
          <span className="text-fg-muted">Hämtar resan …</span>
        )}
      </span>
      {t && stillFromCalendar ? <RejectCalendarPurposeButton tripId={tripId} size="xs" /> : t ? <span className="text-xs text-fg-muted">Rättad</span> : null}
    </li>
  );
}

function NoteLine({ n }: { n: MonthBlocker }) {
  if (n.code === PURPOSE_FROM_CALENDAR) return <CalendarPurposeNote n={n} />;
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

/** "16 nov 10:12" */
function shortDateTime(value: string): string {
  return `${formatDayMonth(value, false)} ${formatTime(value)}`;
}

/** "november" – månadens namn i löptext. */
function monthWord(month: string): string {
  return monthName(Number(month.slice(5, 7)));
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function stateText(s: MonthStatus, job: ExportJob | undefined, current: boolean): React.ReactNode {
  switch (s.state) {
    case "exported": {
      const created = s.export_created_at ?? job?.created_at;
      if (s.closed_early && created) {
        return `Skapat ${shortDateTime(created)}, innan månaden var slut. Vid månadsskiftet kontrolleras månaden igen – ändras något kommer ett nytt underlag och en notis.`;
      }
      return job
        ? `Klart. Underlaget skapades ${job.origin === "month_close" ? "automatiskt " : ""}${formatDateTime(job.created_at)}.`
        : "Klart. Underlaget är skapat.";
    }
    case "stale":
      return current
        ? "Något har ändrats sedan underlaget skapades. Ett nytt underlag skapas efter månadsskiftet, eller nu om du vill."
        : "Något har ändrats sedan underlaget skapades. En ny fil skapas vid nästa kontroll, och du får en notis.";
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

/**
 * Bekräftelsen för "Skapa underlaget nu" (POST /month-close/{månad}/run?force=true). `force`
 * hoppar bara över väntan på månadsslutet – övriga kontroller gäller som vid månadsskiftet.
 */
function CreateNowDialog({
  month,
  renew,
  open,
  onOpenChange,
}: {
  month: string;
  /** Ett underlag finns men något har ändrats ("Skapa nytt underlag nu"). */
  renew: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const run = useRunMonthClose();
  const name = monthWord(month);
  const confirm = () =>
    run.mutate(
      { month, force: true },
      {
        onSuccess: (s) => {
          onOpenChange(false);
          if (s.state === "exported") toast.success(`Underlaget för ${name} är skapat.`);
          else if (s.state === "empty") toast.success(`${capitalize(name)} har inga tjänsteresor – ingen fil behövs.`);
          else if (s.state === "stale") toast.warning("Ett nytt underlag kan inte skapas än – se vad som väntar.");
          else toast.warning("Underlaget kan inte skapas än – se vad som väntar.");
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );
  return (
    <Dialog open={open} onOpenChange={(o) => !run.isPending && onOpenChange(o)}>
      <DialogContent data-testid="month-create-now-dialog">
        <DialogHeader>
          <DialogTitle>{renew ? `Skapa ett nytt underlag för ${name} nu?` : `Skapa underlaget för ${name} nu?`}</DialogTitle>
          <DialogDescription>
            Underlaget skapas nu med de resor som finns i {name}. Samma kontroller som vid månadsskiftet gäller: obesvarade frågor, vetotiden för
            antagna resor och kopplingen till Volvo. Väntar något får du veta vad, och då skapas ingen fil.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm leading-relaxed text-fg-muted">
          <p>Filen och länken skapas direkt. Länken kommer i telefonen, och filen finns här under Export. I filen står när den skapades.</p>
          <p>
            Vid månadsskiftet kontrollerar DriveLog {name} igen. Har en redovisad resa tillkommit eller ändrats skapas ett uppdaterat underlag och
            du får en notis. Finns det då en obesvarad fråga om en tjänsteresa får du veta det. Annars händer inget mer.
          </p>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={run.isPending}>
            Avbryt
          </Button>
          <Button variant="primary" onClick={confirm} loading={run.isPending} data-testid="month-create-now-confirm">
            {run.isPending ? null : <FileCheck aria-hidden />} Skapa underlaget
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MonthRow({ status, current, jobs, selfDriving }: { status: MonthStatus; current: boolean; jobs: ExportJob[]; selfDriving: boolean }) {
  const run = useRunMonthClose();
  const [creating, setCreating] = React.useState(false);
  const job = status.export_id ? jobs.find((j) => j.id === status.export_id) : undefined;
  const blockers = status.blockers.filter((b) => b.code !== "month_not_over");
  const notes = status.notes.filter((n) => !HIDDEN_NOTES.has(n.code) && n.text);
  const assumed = status.review_rows.filter((r) => r.approval_source === "assumed").length;
  const automatic = status.review_rows.length - assumed;
  const title = monthLabel(status.month);
  // "Skapa underlaget nu": bara innevarande månad i självgående läge (run_due körs bara där).
  const notOver = status.blockers.some((b) => b.code === "month_not_over");
  const canCreateNow = current && selfDriving && (((status.state === "open" || status.state === "waiting") && notOver) || status.state === "stale");
  const renew = status.state === "stale";

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
          {canCreateNow ? (
            <Button size="sm" variant="secondary" onClick={() => setCreating(true)} data-testid="month-create-now">
              <FileCheck aria-hidden /> {renew ? "Skapa nytt underlag nu" : "Skapa underlaget nu"}
            </Button>
          ) : null}
        </div>
      </div>
      <p className="mt-1.5 text-[13px] text-fg-muted" data-testid="month-state-text">
        {stateText(status, job, current)}
      </p>
      {/* Hålls monterad även när läget byter efter svaret, så att kvittot hinner visas. */}
      {current && selfDriving ? <CreateNowDialog month={status.month} renew={renew} open={creating} onOpenChange={setCreating} /> : null}
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
  const selfDriving = settings.data?.settings.automation?.mode === "self_driving";

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
          <MonthRow key={m.month} status={m} current={m === current} jobs={exports.data ?? []} selfDriving={selfDriving} />
        ))}
      </ul>
    </Card>
  );
}
