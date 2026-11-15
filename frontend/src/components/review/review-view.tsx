"use client";

import * as React from "react";
import Link from "next/link";
import {
  ArrowRight,
  BadgeCheck,
  Briefcase,
  Check,
  CircleCheckBig,
  Clock,
  ExternalLink,
  Home,
  Merge,
  Sparkles,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { useTripActions } from "@/components/trips/use-trip-actions";
import { api, errorMessage } from "@/lib/api";
import { formatDate, formatNumber, formatPercent } from "@/lib/format";
import { CATEGORY_LABELS, DECISION_SOURCE_LABELS, label, REVIEW_KIND_LABELS, REVIEW_KIND_SHORT } from "@/lib/labels";
import { invalidateJournalData } from "@/lib/queries/keys";
import { HIDDEN_REVIEW_KINDS, useAnswerMerge, useReviewAction, useReviewItems } from "@/lib/queries/review";
import { useTripDetail, useUpdateTrip } from "@/lib/queries/trips";
import type { ReviewItem, Trip } from "@/lib/types";
import { cn, isTypingTarget } from "@/lib/utils";
import { useQueryClient } from "@tanstack/react-query";
import { TripSummary } from "./trip-summary";

/** Ärendetyper där man klassar resan direkt i snabbflödet. */
const TRIP_DECISION_KINDS = new Set(["category", "purpose", "tax_uncertain", "mixed_allocation", "anomaly", "calendar_conflict", "missing_odometer"]);

function hasProposal(t: Trip | null | undefined): boolean {
  return !!t && (t.review_status === "proposed" || t.review_status === "auto") && t.category !== "undecided";
}

// ----------------------------------------------------------------------------- ärendekort

function MergeQuestion({ item, onAnswer, busy }: { item: ReviewItem; onAnswer: (accept: boolean) => void; busy: boolean }) {
  const minutes = item.data.minutes_low != null ? Math.round(item.data.minutes_low) : null;
  const minutesHigh = item.data.minutes_high != null ? Math.round(item.data.minutes_high) : null;
  return (
    <div className="space-y-5">
      <div className="grid items-stretch gap-3 md:grid-cols-[1fr_auto_1fr]">
        {item.first_trip ? (
          <div className="rounded-lg border border-border bg-surface-2/50 p-4">
            <TripSummary trip={item.first_trip} />
          </div>
        ) : null}
        <div className="flex items-center justify-center">
          <div className="rounded-full border border-border bg-surface px-3 py-1.5 text-center text-xs text-fg-muted">
            <Clock className="mx-auto mb-0.5 size-3.5" aria-hidden />
            {minutes != null ? (
              <>
                <span className="num font-medium text-fg">{minutes === minutesHigh || minutesHigh == null ? minutes : `${minutes}–${minutesHigh}`} min</span>
                <br />
                stopp
              </>
            ) : (
              "stopp"
            )}
          </div>
        </div>
        {item.second_trip ? (
          <div className="rounded-lg border border-border bg-surface-2/50 p-4">
            <TripSummary trip={item.second_trip} />
          </div>
        ) : null}
      </div>
      <p className="text-sm text-fg-muted">Var det samma resa med ett kort stopp (t.ex. ett ärende på vägen), eller två separata resor?</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <Button size="xl" variant="primary" onClick={() => onAnswer(true)} disabled={busy} data-testid="merge-yes">
          <Merge aria-hidden /> Ja, slå samman <Kbd className="ml-auto hidden sm:inline-flex border-white/30 bg-white/10 text-current">J</Kbd>
        </Button>
        <Button size="xl" variant="secondary" onClick={() => onAnswer(false)} disabled={busy} data-testid="merge-no">
          <X aria-hidden /> Nej, två resor <Kbd className="ml-auto hidden sm:inline-flex">N</Kbd>
        </Button>
      </div>
    </div>
  );
}

function TripDecision({
  item,
  trip,
  busy,
  onCategory,
  onApprove,
  onPurpose,
}: {
  item: ReviewItem;
  trip: Trip;
  busy: boolean;
  onCategory: (c: "work" | "private") => void;
  onApprove: () => void;
  onPurpose: (text: string) => void;
}) {
  const detail = useTripDetail(trip.id);
  const [purpose, setPurpose] = React.useState("");
  const suggestions = [
    ...(detail.data?.assessment?.purpose ? [detail.data.assessment.purpose] : []),
    ...(detail.data?.purpose_suggestions.map((s) => s.text) ?? []),
  ].filter((s, i, a) => s && a.indexOf(s) === i);
  const proposal = hasProposal(trip);

  return (
    <div className="space-y-5">
      <TripSummary trip={trip} />
      {proposal ? (
        <div className="flex items-start gap-2.5 rounded-lg border border-accent/30 bg-accent-soft/60 p-3 text-sm">
          <Sparkles className="mt-0.5 size-4 shrink-0 text-accent-ink" aria-hidden />
          <p>
            <span className="font-medium">Förslag: {CATEGORY_LABELS[trip.category]}</span>
            {trip.purpose ? <> – {trip.purpose}</> : null}
            <span className="text-fg-muted">
              {" "}
              ({label(DECISION_SOURCE_LABELS, trip.category_source).toLowerCase()}
              {trip.confidence != null ? `, ${formatPercent(trip.confidence)} säkerhet` : ""})
            </span>
          </p>
        </div>
      ) : null}
      <div className={cn("grid gap-2", proposal ? "sm:grid-cols-3" : "sm:grid-cols-2")}>
        <Button size="xl" variant="secondary" onClick={() => onCategory("work")} disabled={busy} className="border-work/40 hover:bg-work-soft" data-testid="decide-work">
          <Briefcase className="text-work" aria-hidden /> Arbete <Kbd className="ml-auto hidden sm:inline-flex">A</Kbd>
        </Button>
        <Button size="xl" variant="secondary" onClick={() => onCategory("private")} disabled={busy} className="border-private/40 hover:bg-private-soft" data-testid="decide-private">
          <Home className="text-private" aria-hidden /> Privat <Kbd className="ml-auto hidden sm:inline-flex">P</Kbd>
        </Button>
        {proposal ? (
          <Button size="xl" variant="primary" onClick={onApprove} disabled={busy} data-testid="decide-approve">
            <BadgeCheck aria-hidden /> Godkänn förslag <Kbd className="ml-auto hidden sm:inline-flex border-white/30 bg-white/10 text-current">G</Kbd>
          </Button>
        ) : null}
      </div>
      {(item.kind === "purpose" || trip.category === "work") && !trip.purpose_confirmed ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (purpose.trim()) onPurpose(purpose.trim());
          }}
          className="space-y-2"
        >
          <label htmlFor="review-purpose" className="text-[13px] font-medium">
            Syfte
          </label>
          <div className="flex gap-2">
            <Input id="review-purpose" value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder={trip.purpose ?? "Vad var syftet med resan?"} maxLength={500} />
            <Button type="submit" variant="secondary" disabled={!purpose.trim() || busy}>
              <Check aria-hidden /> Spara
            </Button>
          </div>
          {suggestions.length ? (
            <div className="flex flex-wrap gap-1.5">
              {suggestions.slice(0, 6).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => onPurpose(s)}
                  className="rounded-full border border-border bg-surface px-2.5 py-1 text-xs hover:border-accent hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-ring"
                >
                  {s}
                </button>
              ))}
            </div>
          ) : null}
        </form>
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------------------------- vy

export function ReviewView() {
  const qc = useQueryClient();
  const list = useReviewItems();
  const answer = useAnswerMerge();
  const reviewAction = useReviewAction();
  const update = useUpdateTrip();
  const actions = useTripActions();
  const [kind, setKind] = React.useState<string>("all");
  const [skipped, setSkipped] = React.useState<string[]>([]);
  const [done, setDone] = React.useState<Set<string>>(new Set());

  const all = React.useMemo(() => (list.data?.items ?? []).filter((i) => !HIDDEN_REVIEW_KINDS.includes(i.kind) && !done.has(i.id)), [list.data, done]);
  const counts = React.useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of all) c[i.kind] = (c[i.kind] ?? 0) + 1;
    return c;
  }, [all]);
  const effectiveKind = kind !== "all" && !counts[kind] ? "all" : kind;
  const queue = React.useMemo(() => {
    const items = all.filter((i) => effectiveKind === "all" || i.kind === effectiveKind);
    return [...items.filter((i) => !skipped.includes(i.id)), ...skipped.map((id) => items.find((i) => i.id === id)).filter(Boolean)] as ReviewItem[];
  }, [all, effectiveKind, skipped]);
  const current = queue[0];
  const busy = answer.isPending || reviewAction.isPending || update.isPending || actions.isPending;

  const finish = React.useCallback((id: string) => {
    setDone((d) => new Set(d).add(id));
    setSkipped((s) => s.filter((x) => x !== id));
  }, []);

  const undoReopen = (id: string) => ({
    label: "Ångra",
    onClick: () =>
      api
        .post(`/review/${id}/reopen`)
        .then(() => {
          setDone((d) => {
            const n = new Set(d);
            n.delete(id);
            return n;
          });
          void invalidateJournalData(qc);
        })
        .catch((e) => toast.error(errorMessage(e))),
  });

  const skip = React.useCallback(() => {
    if (!current) return;
    setSkipped((s) => [...s.filter((x) => x !== current.id), current.id]);
  }, [current]);

  const decideCategory = React.useCallback(
    (c: "work" | "private") => {
      if (!current?.trip) return;
      const item = current;
      finish(item.id);
      actions.setCategory(item.trip!.id, c).catch(() => setDone((d) => { const n = new Set(d); n.delete(item.id); return n; }));
    },
    [current, actions, finish],
  );

  const approve = React.useCallback(() => {
    if (!current?.trip || !hasProposal(current.trip)) return;
    const item = current;
    finish(item.id);
    actions.approve(item.trip!.id).catch(() => setDone((d) => { const n = new Set(d); n.delete(item.id); return n; }));
  }, [current, actions, finish]);

  const savePurpose = (text: string) => {
    if (!current?.trip) return;
    const item = current;
    if (item.kind === "purpose") finish(item.id);
    actions.setPurpose(item.trip!.id, text).catch(() => {});
  };

  const answerMerge = React.useCallback(
    (accept: boolean) => {
      if (!current || current.kind !== "merge") return;
      const item = current;
      finish(item.id);
      answer.mutate(
        { itemId: item.id, accept },
        {
          onSuccess: (res) =>
            toast.success(accept ? "Resorna slogs samman." : "Resorna behålls som två.", {
              action:
                accept && res.trip
                  ? {
                      label: "Ångra",
                      onClick: () =>
                        api
                          .post(`/trips/${res.trip!.id}/unmerge`)
                          .then(() => invalidateJournalData(qc))
                          .catch((e) => toast.error(errorMessage(e))),
                    }
                  : undoReopen(item.id),
            }),
          onError: (e) => {
            setDone((d) => { const n = new Set(d); n.delete(item.id); return n; });
            toast.error(errorMessage(e));
          },
        },
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [current, answer, finish, qc],
  );

  const itemAction = (action: "dismiss" | "snooze" | "resolve") => {
    if (!current) return;
    const item = current;
    finish(item.id);
    reviewAction.mutate(
      { itemId: item.id, action },
      {
        onSuccess: () =>
          toast.success(action === "dismiss" ? "Ärendet är avfärdat." : action === "snooze" ? "Uppskjutet ett dygn." : "Markerat som hanterat.", {
            action: undoReopen(item.id),
          }),
        onError: (e) => {
          setDone((d) => { const n = new Set(d); n.delete(item.id); return n; });
          toast.error(errorMessage(e));
        },
      },
    );
  };

  // Tangentbordsgenvägar: A, P, G, J, N, →
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e.target) || !current || busy) return;
      if (document.querySelector("[role=dialog]")) return;
      const k = e.key.toLowerCase();
      if (current.kind === "merge") {
        if (k === "j") answerMerge(true);
        else if (k === "n") answerMerge(false);
      } else if (current.trip && TRIP_DECISION_KINDS.has(current.kind)) {
        if (k === "a") decideCategory("work");
        else if (k === "p") decideCategory("private");
        else if (k === "g") approve();
      }
      if (e.key === "ArrowRight") skip();
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, busy, answerMerge, decideCategory, approve, skip]);

  const tabs = Object.keys(REVIEW_KIND_LABELS).filter((k) => counts[k]);
  const total = all.length;

  return (
    <>
      <PageHeader
        title="Granskning"
        description="Gå igenom det som behöver ditt beslut – ett ärende i taget, utan att öppna varje resa."
        actions={total ? <Badge tone="warn" className="text-sm num">{formatNumber(total, 0)} att granska</Badge> : null}
      />

      {list.isError ? (
        <Card>
          <ErrorState error={list.error} onRetry={() => list.refetch()} />
        </Card>
      ) : !list.data ? (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]" role="status" aria-label="Laddar ärenden">
          <Skeleton className="h-80" />
          <Skeleton className="h-80" />
        </div>
      ) : total === 0 ? (
        <Card>
          <EmptyState
            icon={<CircleCheckBig />}
            title="Allt är granskat"
            description="Det finns inga öppna ärenden just nu. Nya resor som behöver ditt beslut dyker upp här."
            action={
              <Button asChild>
                <Link href="/journal">Till körjournalen</Link>
              </Button>
            }
          />
        </Card>
      ) : (
        <>
          {tabs.length > 1 ? (
            <div role="tablist" aria-label="Ärendetyp" className="no-scrollbar mb-4 flex gap-1.5 overflow-x-auto">
              {["all", ...tabs].map((k) => {
                const active = effectiveKind === k;
                return (
                  <button
                    key={k}
                    role="tab"
                    aria-selected={active}
                    onClick={() => setKind(k)}
                    className={cn(
                      "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[13px] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-ring",
                      active ? "border-fg bg-fg text-bg" : "border-border bg-surface text-fg-muted hover:text-fg",
                    )}
                  >
                    {k === "all" ? "Alla" : label(REVIEW_KIND_SHORT, k)}
                    <span className={cn("num text-xs", active ? "opacity-80" : "text-fg-subtle")}>{k === "all" ? total : counts[k]}</span>
                  </button>
                );
              })}
            </div>
          ) : null}

          <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
            <section aria-label="Aktuellt ärende" aria-live="polite">
              {current ? (
                <Card key={current.id} className="animate-pop-in p-5 sm:p-6" data-testid="review-current">
                  <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                    <Badge tone={current.kind === "merge" ? "accent" : current.kind === "category" ? "undecided" : "neutral"}>
                      {label(REVIEW_KIND_LABELS, current.kind)}
                    </Badge>
                    <span className="text-xs text-fg-muted num">
                      {queue.length > 1 ? `${formatNumber(queue.length - 1, 0)} till efter detta` : "Sista ärendet"}
                    </span>
                  </div>
                  <h2 className="mb-1 text-lg font-semibold tracking-tight">{current.title}</h2>
                  {current.detail && current.kind !== "merge" ? <p className="mb-4 text-sm text-fg-muted">{current.detail}</p> : <div className="mb-4" />}

                  {current.kind === "merge" ? (
                    <MergeQuestion item={current} onAnswer={answerMerge} busy={busy} />
                  ) : current.trip && TRIP_DECISION_KINDS.has(current.kind) ? (
                    <TripDecision
                      key={current.trip.id}
                      item={current}
                      trip={current.trip}
                      busy={busy}
                      onCategory={decideCategory}
                      onApprove={approve}
                      onPurpose={savePurpose}
                    />
                  ) : (
                    <div className="space-y-4">
                      {current.trip ? <TripSummary trip={current.trip} /> : null}
                      <Button variant="primary" size="lg" onClick={() => itemAction("resolve")} disabled={busy}>
                        <Check aria-hidden /> Markera som hanterat
                      </Button>
                    </div>
                  )}

                  <div className="mt-6 flex flex-wrap items-center gap-1.5 border-t border-border pt-4">
                    <Button variant="ghost" size="sm" onClick={skip} disabled={queue.length < 2}>
                      Hoppa över <ArrowRight aria-hidden />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => itemAction("snooze")} disabled={busy}>
                      <Clock aria-hidden /> Skjut upp
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => itemAction("dismiss")} disabled={busy}>
                      <X aria-hidden /> Avfärda
                    </Button>
                    {current.trip ? (
                      <Button variant="ghost" size="sm" asChild className="ml-auto">
                        <Link href={`/journal/${current.trip.id}`}>
                          Öppna resan <ExternalLink aria-hidden />
                        </Link>
                      </Button>
                    ) : null}
                  </div>
                </Card>
              ) : null}
              <p className="mt-3 hidden flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-muted sm:flex" aria-label="Kortkommandon">
                <span className="inline-flex items-center gap-1.5"><Kbd>A</Kbd> Arbete</span>
                <span className="inline-flex items-center gap-1.5"><Kbd>P</Kbd> Privat</span>
                <span className="inline-flex items-center gap-1.5"><Kbd>G</Kbd> Godkänn förslag</span>
                <span className="inline-flex items-center gap-1.5"><Kbd>J</Kbd>/<Kbd>N</Kbd> Slå samman ja/nej</span>
                <span className="inline-flex items-center gap-1.5"><Kbd>→</Kbd> Nästa</span>
              </p>
            </section>

            <aside aria-label="Kö" className="hidden lg:block">
              <Card className="overflow-hidden">
                <h2 className="border-b border-border px-4 py-3 text-[13px] font-semibold">I kön</h2>
                <ol className="scrollbar-thin max-h-[560px] divide-y divide-border overflow-y-auto">
                  {queue.map((i, idx) => (
                    <li key={i.id} className={cn("px-4 py-2.5 text-sm", idx === 0 && "bg-accent-soft/50")}>
                      <p className="text-xs text-fg-muted">{label(REVIEW_KIND_SHORT, i.kind)}{skipped.includes(i.id) ? " · överhoppad" : ""}</p>
                      <p className="truncate">
                        {i.trip ? (
                          <>
                            <span className="num text-xs text-fg-muted">{formatDate(i.trip.started_at ?? i.trip.sort_time)}</span> {i.trip.end_address ?? i.title}
                          </>
                        ) : (
                          i.title
                        )}
                      </p>
                    </li>
                  ))}
                </ol>
              </Card>
            </aside>
          </div>
        </>
      )}
    </>
  );
}
