"use client";

import * as React from "react";
import Link from "next/link";
import { BookOpen, CircleAlert, CircleCheckBig, Clock, ClipboardCheck, Info, SearchX } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDate, formatDateTime, formatRelative, formatTime, localDateISO } from "@/lib/format";
import { label, NOTIFICATION_KIND_LABELS } from "@/lib/labels";
import type { Question, RespondResult } from "@/lib/notifications-types";
import { useQuestion, useQuestionTrip, useRespond } from "@/lib/queries/notifications";
import { useUpdateTrip } from "@/lib/queries/trips";
import type { TripDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Answers, PurposeInput } from "./answers";
import { QuickTripCard, QuickTripCardSkeleton } from "./trip-card";

// ----------------------------------------------------------------------------- texter

/** Rubriken: själva frågan, kort och tydlig. */
function headline(q: Question): string {
  switch (q.kind) {
    case "classify":
      return isSelfDrivingClassify(q) ? "Tjänst eller privat?" : "Arbete eller privat?";
    case "purpose":
      return "Vad var syftet med resan?";
    case "merge":
      return "Slå samman resorna?";
    case "test":
      return "Testnotis";
    case "company":
      return "Vilket företag var det?";
    default:
      return q.title;
  }
}

/** Notistyperna från självgående läge (1.1.0) som inte finns i den gemensamma etikettlistan. */
const EXTRA_KIND_LABELS: Record<string, string> = {
  assumed: "Antagande",
  company: "Företag",
  alert: "Larm",
  monthly: "Månadsskifte",
  release: "Ny version",
};

/** Notiser som bara berättar något (inga svar): länkarna visas i stället för svarsknappar. */
const INFORMATIONAL_KINDS = new Set(["digest", "weekly", "daily", "alert", "monthly", "release"]);

/** Klassningsfrågan i självgående läge har ärendet i samma svar. */
function isSelfDrivingClassify(q: Question): boolean {
  return q.kind === "classify" && !q.actions.some((a) => a.key === "WORK");
}

/** Underrubrik: frågans text när den säger mer än rubriken (resan visas separat). */
function subline(q: Question): string | null {
  if (q.kind === "merge") return q.detail_message ?? q.message;
  if (q.kind === "classify" || q.kind === "purpose" || q.kind === "company") return null;
  return q.detail_message ?? q.message;
}

function whenText(iso: string): string {
  return formatDate(iso) === localDateISO() ? `i dag ${formatTime(iso)}` : formatDateTime(iso);
}

function newKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

// ----------------------------------------------------------------------------- delar

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto w-full max-w-xl">{children}</div>;
}

function Header({ q }: { q: Question }) {
  const sent = q.sent_at ?? q.created_at;
  const sub = subline(q);
  return (
    <header className="mb-5">
      <p className="text-xs font-medium text-fg-subtle">
        {label({ ...NOTIFICATION_KIND_LABELS, ...EXTRA_KIND_LABELS }, q.kind)} · {formatRelative(sent)}
      </p>
      <h1 className="mt-1 text-[26px] leading-tight font-semibold tracking-tight text-fg sm:text-[28px]">{headline(q)}</h1>
      {sub ? <p className="mt-2 text-[15px] leading-relaxed text-fg-muted">{sub}</p> : null}
    </header>
  );
}

function Notice({ tone, icon, children }: { tone: "info" | "warn"; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div
      className={cn(
        "mb-4 flex items-start gap-2.5 rounded-lg px-3.5 py-3 text-sm leading-relaxed [&_svg]:mt-0.5 [&_svg]:size-4 [&_svg]:shrink-0",
        tone === "warn" ? "bg-warn-soft text-warn-ink" : "bg-surface-2 text-fg-muted",
      )}
    >
      {icon}
      <div>{children}</div>
    </div>
  );
}

/** Resorna som frågan gäller: en resa, eller två vid sammanslagning. */
function Trips({ q, detail }: { q: Question; detail: TripDetail | undefined }) {
  if (q.kind === "merge" && q.merge_trips.length) {
    return (
      <div className="space-y-2">
        {q.merge_trips.map((t) => (
          <QuickTripCard key={t.id} trip={t} showCategory={false} />
        ))}
      </div>
    );
  }
  if (q.trip) return <QuickTripCard trip={q.trip} full={detail?.trip} showPurpose={q.kind !== "purpose"} />;
  return null;
}

/** Notisens egna länkknappar (t.ex. "Öppna underlaget" och "Se antagna" i månadsnotisen). */
function NoticeLinks({ q }: { q: Question }) {
  const linked = q.actions.filter((a) => a.uri);
  if (!linked.length) return null;
  return (
    <div className="mt-6 flex flex-col gap-2 sm:flex-row sm:justify-center">
      {linked.map((a, i) => {
        const href = a.uri ?? "/";
        let path = href;
        try {
          const url = new URL(href);
          path = `${url.pathname}${url.search}`;
        } catch {
          // redan en relativ sökväg
        }
        return (
          <Button key={a.key} asChild variant={i === 0 ? "primary" : "secondary"} size="lg" className="rounded-xl">
            <Link href={path}>{a.title}</Link>
          </Button>
        );
      })}
    </div>
  );
}

/** Länkar vidare: resan i körjournalen och granskningen. */
function Links({ tripId, primary = false }: { tripId: string | null; primary?: boolean }) {
  return (
    <div className="mt-6 flex flex-col gap-2 sm:flex-row sm:justify-center">
      {tripId ? (
        <Button asChild variant={primary ? "primary" : "secondary"} size="lg" className="rounded-xl">
          <Link href={`/journal/${tripId}`}>
            <BookOpen aria-hidden /> Öppna i körjournalen
          </Link>
        </Button>
      ) : null}
      <Button asChild variant={primary && !tripId ? "primary" : tripId ? "ghost" : "secondary"} size="lg" className="rounded-xl">
        <Link href="/review">
          <ClipboardCheck aria-hidden /> Till granskningen
        </Link>
      </Button>
    </div>
  );
}

// ----------------------------------------------------------------------------- kvitto

/** Följdfråga efter "Arbete" – tjänsteresor behöver ett syfte i körjournalen. */
function PurposeFollowUp({ tripId, detail }: { tripId: string; detail: TripDetail | undefined }) {
  const update = useUpdateTrip();
  const [saved, setSaved] = React.useState<string | null>(null);
  const suggestions = React.useMemo(() => {
    const out: string[] = [];
    const current = detail?.trip.purpose?.trim();
    if (current && !detail?.trip.purpose_confirmed) out.push(current);
    for (const s of detail?.purpose_suggestions ?? []) if (s.text && !out.includes(s.text)) out.push(s.text);
    return out.slice(0, 3);
  }, [detail]);

  if (detail?.trip.purpose_confirmed && detail.trip.purpose && !saved) return null;

  const save = (text: string) =>
    update.mutate(
      { id: tripId, patch: { purpose: text } },
      {
        onSuccess: () => {
          setSaved(text);
          toast.success("Syftet sparades.");
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );

  if (saved) {
    return (
      <p className="mt-5 rounded-lg bg-surface-2 px-4 py-3 text-sm text-fg-muted" role="status">
        Syfte: <span className="font-medium text-fg">{saved}</span>
      </p>
    );
  }
  return (
    <div className="mt-6 border-t border-border pt-5 text-left">
      <h2 className="text-base font-semibold text-fg">Vad var syftet?</h2>
      <p className="mt-0.5 text-sm text-fg-muted">Tjänsteresor behöver ett syfte i körjournalen.</p>
      {suggestions.length ? (
        <div className="mt-3 grid gap-2">
          {suggestions.map((s) => (
            <Button
              key={s}
              size="lg"
              className="h-auto min-h-12 justify-start rounded-xl py-3 text-left text-base whitespace-normal"
              onClick={() => save(s)}
              disabled={update.isPending}
              loading={update.isPending && update.variables?.patch.purpose === s}
            >
              {s}
            </Button>
          ))}
        </div>
      ) : null}
      <div className="mt-3">
        <PurposeInput id="quick-follow-purpose" onSave={save} loading={update.isPending && !suggestions.includes(update.variables?.patch.purpose ?? "")} disabled={update.isPending} />
      </div>
    </div>
  );
}

function Receipt({
  q,
  result,
  detail,
  onBack,
}: {
  q: Question;
  result: RespondResult;
  detail: TripDetail | undefined;
  onBack: () => void;
}) {
  const o = result.outcome;
  const tripId = result.merged_trip_id ?? result.trip_id ?? q.trip_id ?? q.merge_trips[0]?.id ?? null;
  const meta =
    o === "applied"
      ? { icon: <CircleCheckBig />, tone: "bg-ok-soft text-ok-ink", title: "Sparat" }
      : o === "snoozed"
        ? { icon: <Clock />, tone: "bg-surface-2 text-fg-muted", title: "Frågan skjuts upp" }
        : o === "duplicate"
          ? { icon: <Info />, tone: "bg-surface-2 text-fg-muted", title: "Redan besvarad" }
          : o === "expired"
            ? { icon: <Clock />, tone: "bg-warn-soft text-warn-ink", title: "Frågan har gått ut" }
            : { icon: <CircleAlert />, tone: "bg-err-soft text-err-ink", title: "Det gick inte att spara" };

  let text = result.message;
  if (o === "snoozed" && result.snoozed_until) text = `Du får frågan igen ${whenText(result.snoozed_until)}.`;
  if (o === "duplicate" && q.answer?.message) text = `${result.message} ${q.answer.message}`;
  const askPurpose = o === "applied" && q.kind === "classify" && result.category === "work" && !!tripId && !result.follow_up && !result.purpose;
  const canRetry = o === "invalid" && result.code !== "not_active" && q.answerable;

  return (
    <Card className="p-6 text-center sm:p-8" data-testid="quick-receipt">
      <div role="status" aria-live="polite">
        <div className={cn("mx-auto flex size-14 items-center justify-center rounded-full [&_svg]:size-7", meta.tone)} aria-hidden>
          {meta.icon}
        </div>
        <h2 className="mt-4 text-xl font-semibold tracking-tight text-fg">{meta.title}</h2>
        <p className="mx-auto mt-1.5 max-w-sm text-[15px] leading-relaxed text-fg-muted">{text}</p>
      </div>
      {askPurpose ? <PurposeFollowUp tripId={tripId} detail={detail} /> : null}
      {canRetry ? (
        <Button variant="secondary" size="lg" className="mt-5 rounded-xl" onClick={onBack}>
          Svara igen
        </Button>
      ) : null}
      {q.kind !== "test" ? <Links tripId={tripId} primary={o === "invalid" || o === "expired"} /> : null}
    </Card>
  );
}

/** Frågan är redan stängd (besvarad, hanterad i körjournalen eller kunde inte levereras). */
function Closed({ q }: { q: Question }) {
  const answered = q.status === "answered";
  const title = answered ? "Frågan är redan besvarad" : q.status === "cancelled" ? "Frågan är redan hanterad" : "Frågan är inte längre aktiv";
  const text = answered
    ? [q.answer?.message, q.answered_at ? `Besvarad ${formatRelative(q.answered_at)}.` : null].filter(Boolean).join(" ")
    : q.status === "cancelled"
      ? "Resan har redan granskats i körjournalen, så frågan behövs inte längre."
      : "Frågan kunde inte levereras. Granska resan i körjournalen i stället.";
  return (
    <Card className="p-6 text-center sm:p-8" data-testid="quick-closed">
      <div className={cn("mx-auto flex size-14 items-center justify-center rounded-full [&_svg]:size-7", answered ? "bg-ok-soft text-ok-ink" : "bg-surface-2 text-fg-muted")} aria-hidden>
        {answered ? <CircleCheckBig /> : <Info />}
      </div>
      <h2 className="mt-4 text-xl font-semibold tracking-tight text-fg">{title}</h2>
      {text ? <p className="mx-auto mt-1.5 max-w-sm text-[15px] leading-relaxed text-fg-muted">{text}</p> : null}
      <Links tripId={q.trip_id} />
    </Card>
  );
}

// ----------------------------------------------------------------------------- vy

function LoadingView() {
  return (
    <Shell>
      <div className="mb-5" role="status" aria-label="Laddar frågan">
        <Skeleton className="h-3.5 w-32" />
        <Skeleton className="mt-2.5 h-8 w-64" />
      </div>
      <QuickTripCardSkeleton />
      <div className="mt-6 grid grid-cols-2 gap-3">
        <Skeleton className="h-16 rounded-xl" />
        <Skeleton className="h-16 rounded-xl" />
      </div>
      <Skeleton className="mt-3 h-11 rounded-xl" />
    </Shell>
  );
}

/**
 * Mobil snabbvy för en notisfråga (/m/q/[id]). Öppnas från notisen på telefonen:
 * frågan stort, resan i korthet och tumvänliga svarsknappar. Svaret skickas med en
 * idempotensnyckel per val, så en dubbeltryckning eller ett omförsök sparar bara en gång.
 */
export function QuickQuestionView({ publicId }: { publicId: string }) {
  const query = useQuestion(publicId);
  const q = query.data;
  const respond = useRespond(publicId);
  const [result, setResult] = React.useState<RespondResult | null>(null);
  // Resans detaljer behövs bara som reserv (äldre backend utan platsetiketter/tidskvalitet i frågan)
  // och för syftesförslagen i följdfrågan efter ett svar.
  const needsDetail = q?.trip?.state === "active" && (q.trip.start_label === undefined || q.trip.start_time_quality === undefined || result !== null);
  const detail = useQuestionTrip(needsDetail ? q.trip_id : null);
  const keys = React.useRef(new Map<string, string>());

  const answer = (action: string, replyText?: string, always?: boolean) => {
    const id = `${action}|${replyText ?? ""}|${always ? "1" : ""}`;
    const key = keys.current.get(id) ?? newKey();
    keys.current.set(id, key);
    respond.mutate(
      { action, reply_text: replyText ?? null, idempotency_key: key, always: !!always },
      {
        onSuccess: (res) => {
          setResult(res);
          // Kvittot visas på sidan (role="status") – även när svaret inte gick igenom.
          // Ett nytt försök efter ett ogiltigt svar ska behandlas som ett nytt svar.
          if (res.outcome === "invalid") keys.current.delete(id);
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );
  };

  if (query.isPending) return <LoadingView />;

  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    return (
      <Shell>
        <h1 className="sr-only">Snabbfråga</h1>
        <Card>
          {notFound ? (
            <EmptyState
              icon={<SearchX />}
              title="Frågan hittades inte"
              description="Länken kan vara gammal, eller så har frågan tagits bort. Resorna som behöver svar finns i granskningen."
              action={
                <Button asChild variant="primary">
                  <Link href="/review">
                    <ClipboardCheck aria-hidden /> Till granskningen
                  </Link>
                </Button>
              }
            />
          ) : (
            <ErrorState error={query.error} onRetry={() => query.refetch()} title="Frågan kunde inte hämtas" />
          )}
        </Card>
      </Shell>
    );
  }

  const data = query.data;
  const informational = INFORMATIONAL_KINDS.has(data.kind);
  const open = data.answerable && !informational && data.status !== "answered";
  const pending = respond.isPending ? (respond.variables?.action ?? null) : null;

  return (
    <Shell>
      <Header q={data} />
      {result ? (
        <Receipt q={data} result={result} detail={detail.data} onBack={() => setResult(null)} />
      ) : (
        <>
          {open && data.expired ? (
            <Notice tone="warn" icon={<Clock />}>
              Frågan har gått ut på telefonen. Du kan fortfarande svara här, eller granska resan i körjournalen.
            </Notice>
          ) : null}
          <Trips q={data} detail={detail.data} />
          {open ? (
            <>
              <div className="mt-6">
                <Answers question={data} onAnswer={answer} pending={pending} pendingText={respond.isPending ? respond.variables?.reply_text : null} />
              </div>
              {data.kind !== "test" ? <Links tripId={data.trip_id ?? data.merge_trips[0]?.id ?? null} /> : null}
            </>
          ) : informational ? (
            data.actions.some((a) => a.uri) ? <NoticeLinks q={data} /> : <Links tripId={null} primary />
          ) : (
            <div className="mt-4">
              <Closed q={data} />
            </div>
          )}
        </>
      )}
    </Shell>
  );
}

