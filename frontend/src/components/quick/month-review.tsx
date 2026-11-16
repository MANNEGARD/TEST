"use client";

import * as React from "react";
import Link from "next/link";
import { CalendarDays, CalendarX2, Check, CircleCheckBig, FileDown, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDate, milNumber } from "@/lib/format";
import type { ReviewRow, ReviewVerdict } from "@/lib/notifications-types";
import { PURPOSE_FROM_CALENDAR } from "@/lib/labels";
import { useMonthReview, useReviewVerdict } from "@/lib/queries/notifications";
import { useRejectCalendarPurpose } from "@/lib/queries/trips";

const MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"];

/** Skälet som ägaren ser (byggkontraktet A.2). */
const SOURCE_LABELS: Record<string, string> = {
  rule: "Din regel",
  history: "Som de 3 senaste gångerna",
  return_trip: "Hemresa",
  commute: "Resa till arbetet",
  private_place: "Privat plats",
  assumed: "Antaget kundbesök",
  home_office: "Hem ↔ kontor",
  lunch: "Lunch",
};

function monthTitle(month: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(month);
  if (!m) return month;
  const name = MONTHS[Number(m[2]) - 1];
  return name ? `${name[0].toUpperCase()}${name.slice(1)} ${m[1]}` : month;
}

function Row({ row, done, onVerdict, pending, fromCalendar, onRejectCalendar, rejecting }: {
  row: ReviewRow;
  done: string | undefined;
  onVerdict: (verdict: ReviewVerdict) => void;
  pending: ReviewVerdict | null;
  /** Ärendet sattes ur en kalenderbokning utan fråga (beslut A). */
  fromCalendar: boolean;
  onRejectCalendar: () => void;
  rejecting: boolean;
}) {
  const assumed = row.approval_source === "assumed";
  const reason = row.reason || SOURCE_LABELS[row.approval_source] || "Godkänd automatiskt";
  return (
    <li className="rounded-xl border border-border bg-surface p-4 shadow-sm" data-testid="month-review-row">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-fg-muted">
          <span className="num">{row.date ? formatDate(row.date) : "Okänt datum"}</span>
          <span className="mx-1.5" aria-hidden>·</span>
          <span className={assumed ? "font-medium text-warn-ink" : undefined}>{assumed ? "Antaget" : `Godkänd automatiskt · ${reason}`}</span>
        </p>
        <p className="shrink-0 text-right">
          <span className="num font-semibold text-fg">{milNumber(row.distance_km)}</span>
          <span className="ml-1 text-sm text-fg-muted">mil</span>
        </p>
      </div>
      <p className="mt-2 text-base font-medium text-fg">{row.route}</p>
      <p className="mt-0.5 text-sm text-fg-muted">
        {row.category === "work" ? (row.purpose ?? "Tjänst") : "Privat"}
        {assumed && row.reason ? ` · ${row.reason}` : null}
      </p>
      {fromCalendar ? (
        <p className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-accent-ink" data-testid="month-review-calendar">
          <CalendarDays className="size-3.5" aria-hidden /> Ärende ur kalendern
        </p>
      ) : null}
      {done ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-ok-ink" role="status">
          <CircleCheckBig className="size-4" aria-hidden /> {done}
        </p>
      ) : (
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button variant="secondary" size="lg" className="rounded-xl" onClick={() => onVerdict("ok")} loading={pending === "ok"} disabled={pending !== null}>
            {pending === "ok" ? null : <Check aria-hidden />} Stämmer
          </Button>
          <Button variant="secondary" size="lg" className="rounded-xl" onClick={() => onVerdict("wrong")} loading={pending === "wrong"} disabled={pending !== null}>
            {pending === "wrong" ? null : <X aria-hidden />} Fel
          </Button>
          {fromCalendar ? (
            <Button
              variant="ghost"
              size="lg"
              className="col-span-2 rounded-xl"
              onClick={onRejectCalendar}
              loading={rejecting}
              disabled={pending !== null}
              data-testid="reject-calendar-purpose"
            >
              {rejecting ? null : <CalendarX2 aria-hidden />} Ärendet stämmer inte
            </Button>
          ) : null}
        </div>
      )}
    </li>
  );
}

/**
 * Månadens genomgång (/m/month/[month]): rader som automatiken godkände eller antog, med
 * skälet, och knapparna "Stämmer" och "Fel". Kräver inloggning, som resten av appen.
 */
export function MonthReviewView({ month }: { month: string }) {
  const query = useMonthReview();
  const verdict = useReviewVerdict();
  const reject = useRejectCalendarPurpose();
  const [done, setDone] = React.useState<Record<string, string>>({});
  const title = monthTitle(month);

  if (query.isPending) {
    return (
      <div className="mx-auto w-full max-w-xl space-y-3" role="status" aria-label="Laddar månaden">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-28 rounded-xl" />
        <Skeleton className="h-28 rounded-xl" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="mx-auto w-full max-w-xl">
        <Card>
          <ErrorState error={query.error} onRetry={() => query.refetch()} title="Månaden kunde inte hämtas" />
        </Card>
      </div>
    );
  }

  const status = query.data.months.find((m) => m.month === month);
  const rows = status?.review_rows ?? [];
  const fromCalendar = new Set(status?.notes.find((n) => n.code === PURPOSE_FROM_CALENDAR)?.trip_ids ?? []);
  const rejectCalendar = (row: ReviewRow) =>
    reject.mutate(row.trip_id, {
      onSuccess: () => setDone((d) => ({ ...d, [row.trip_id]: "Kalenderns ärende är borttaget. Resan väntar på ditt ärende i Granskning." })),
      onError: (e) => toast.error(errorMessage(e)),
    });
  const rowProps = (r: ReviewRow) => ({
    fromCalendar: fromCalendar.has(r.trip_id),
    onRejectCalendar: () => rejectCalendar(r),
    rejecting: reject.isPending && reject.variables === r.trip_id,
  });
  const assumed = rows.filter((r) => r.approval_source === "assumed");
  const others = rows.filter((r) => r.approval_source !== "assumed");
  const pendingFor = (tripId: string): ReviewVerdict | null =>
    verdict.isPending && verdict.variables?.tripId === tripId ? verdict.variables.verdict : null;
  const send = (row: ReviewRow, v: ReviewVerdict) =>
    verdict.mutate(
      { tripId: row.trip_id, verdict: v },
      {
        onSuccess: (res) => setDone((d) => ({ ...d, [row.trip_id]: res.message })),
        onError: (e) => toast.error(errorMessage(e)),
      },
    );

  return (
    <div className="mx-auto w-full max-w-xl">
      <header className="mb-5">
        <p className="text-xs font-medium text-fg-subtle">Månadens genomgång</p>
        <h1 className="mt-1 text-[26px] leading-tight font-semibold tracking-tight text-fg">{title}</h1>
        <p className="mt-2 text-[15px] leading-relaxed text-fg-muted">
          Raderna nedan avgjorde DriveLog utan att fråga dig. Se över dem innan du skickar underlaget.
        </p>
      </header>
      {!status ? (
        <Card>
          <EmptyState title="Månaden finns inte här" description="Genomgången visar innevarande och föregående månad." />
        </Card>
      ) : rows.length === 0 ? (
        <Card>
          <EmptyState title="Inget att se över" description="Inga rader är antagna eller godkända automatiskt den här månaden." />
        </Card>
      ) : (
        <div className="space-y-6">
          {assumed.length ? (
            <section>
              <h2 className="mb-2 text-base font-semibold text-fg">Antagna ({assumed.length})</h2>
              <ul className="space-y-2">
                {assumed.map((r) => (
                  <Row key={r.trip_id} row={r} done={done[r.trip_id]} onVerdict={(v) => send(r, v)} pending={pendingFor(r.trip_id)} {...rowProps(r)} />
                ))}
              </ul>
            </section>
          ) : null}
          {others.length ? (
            <section>
              <h2 className="mb-2 text-base font-semibold text-fg">Godkända automatiskt ({others.length})</h2>
              <ul className="space-y-2">
                {others.map((r) => (
                  <Row key={r.trip_id} row={r} done={done[r.trip_id]} onVerdict={(v) => send(r, v)} pending={pendingFor(r.trip_id)} {...rowProps(r)} />
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
      <div className="mt-6 flex justify-center">
        <Button asChild variant="primary" size="lg" className="rounded-xl">
          <Link href="/export">
            <FileDown aria-hidden /> Till underlaget
          </Link>
        </Button>
      </div>
    </div>
  );
}
