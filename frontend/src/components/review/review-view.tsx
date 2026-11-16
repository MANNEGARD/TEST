"use client";

import * as React from "react";
import Link from "next/link";
import {
  AlertTriangle,
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
import { invalidateJournalData, qk } from "@/lib/queries/keys";
import { HIDDEN_REVIEW_KINDS, useAnswerMerge, useReviewAction, useReviewItems } from "@/lib/queries/review";
import { useTripDetail } from "@/lib/queries/trips";
import type { CategoryCode, ReviewItem, Trip } from "@/lib/types";
import { cn, isTypingTarget } from "@/lib/utils";
import { useIsMutating, useQueryClient } from "@tanstack/react-query";
import { AiReviewDialog } from "@/components/trips/ai-review-dialog";
import { TripSummary } from "./trip-summary";

/** Ärendetyper där man klassar resan direkt i snabbflödet. */
const TRIP_DECISION_KINDS = new Set(["category", "purpose", "tax_uncertain", "mixed_allocation", "anomaly", "calendar_conflict", "missing_odometer"]);

function hasProposal(t: Trip | null | undefined): boolean {
  return !!t && (t.review_status === "proposed" || t.review_status === "auto") && t.category !== "undecided";
}

// ----------------------------------------------------------------------------- ärendekort

/** Så tätt kommer klicken i ett dubbelklick; ett snabbt tryck på nästa ärende kommer senare. */
const DOUBLE_CLICK_MS = 300;
let lastClickAt = -Infinity;

/** Ett klick på en knapp: den andra halvan av ett dubbelklick hamnar annars på nästa ärende.
 *  Webbläsaren räknar även två snabba klick eller tryck på samma ställe som ett dubbelklick
 *  (detail 2) när nästa ärendes knapp hamnar där den förra låg – därför avgör tiden mellan
 *  klicken, och tryck på en pekskärm (där man inte dubbelklickar) räknas alltid. */
function once(e: React.MouseEvent, fn: () => void) {
  const touch = "pointerType" in e.nativeEvent && e.nativeEvent.pointerType === "touch";
  const double = !touch && e.detail > 1 && e.timeStamp - lastClickAt < DOUBLE_CLICK_MS;
  lastClickAt = e.timeStamp;
  if (!double) fn();
}

function MergeQuestion({ item, onAnswer }: { item: ReviewItem; onAnswer: (accept: boolean) => void }) {
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
        <Button size="xl" variant="primary" onClick={(e) => once(e, () => onAnswer(true))} data-testid="merge-yes">
          <Merge aria-hidden /> Ja, slå samman <Kbd className="ml-auto hidden sm:inline-flex border-white/30 bg-white/10 text-current">J</Kbd>
        </Button>
        <Button size="xl" variant="secondary" onClick={(e) => once(e, () => onAnswer(false))} data-testid="merge-no">
          <X aria-hidden /> Nej, två resor <Kbd className="ml-auto hidden sm:inline-flex">N</Kbd>
        </Button>
      </div>
    </div>
  );
}

function TripDecision({
  item,
  trip,
  onCategory,
  onApprove,
  onPurpose,
}: {
  item: ReviewItem;
  trip: Trip;
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
        <Button size="xl" variant="secondary" onClick={(e) => once(e, () => onCategory("work"))} className="border-work/40 hover:bg-work-soft" data-testid="decide-work">
          <Briefcase className="text-work" aria-hidden /> Arbete <Kbd className="ml-auto hidden sm:inline-flex">A</Kbd>
        </Button>
        <Button size="xl" variant="secondary" onClick={(e) => once(e, () => onCategory("private"))} className="border-private/40 hover:bg-private-soft" data-testid="decide-private">
          <Home className="text-private" aria-hidden /> Privat <Kbd className="ml-auto hidden sm:inline-flex">P</Kbd>
        </Button>
        {proposal ? (
          <Button size="xl" variant="primary" onClick={(e) => once(e, onApprove)} data-testid="decide-approve">
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
            <Button type="submit" variant="secondary" disabled={!purpose.trim()}>
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
  const actions = useTripActions();
  const [kind, setKind] = React.useState<string>("all");
  const [aiReview, setAiReview] = React.useState(false);
  const [skipped, setSkipped] = React.useState<string[]>([]);
  // Besvarade ärenden döljs direkt, utan att vänta på servern. Misslyckas svaret läggs ärendet
  // tillbaka först i kön med felet synligt (front, failed).
  const [done, setDone] = React.useState<Set<string>>(new Set());
  const [front, setFront] = React.useState<string[]>([]);
  const [failed, setFailed] = React.useState<Record<string, string>>({});
  // När servern har sparat svaret gäller dess lista igen: ett ärende som fortfarande är öppet
  // (t.ex. "Syfte saknas" efter Arbete) kommer tillbaka när listan har hämtats om efter svaret.
  const [saved, setSaved] = React.useState<Record<string, number>>({});
  // Samma ärende besvaras aldrig två gånger, även om två tangenttryck hinner komma innan
  // nästa ärende visas (svaret binds till ärendet som visades när det gavs).
  const answered = React.useRef(new Set<string>());
  // Svar som är skickade men inte sparade än: syns på sidan, och ägaren varnas om fliken
  // stängs innan de har kommit fram.
  const saving = useIsMutating();
  React.useEffect(() => {
    if (!saving) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [saving]);

  // Kön hålls stilla medan ägaren svarar: listan hämtas om efter varje svar och medan
  // förslagen räknas om, men nya ärenden läggs sist (i den ordning de först syntes) och
  // ärendet som visas byts aldrig ut under muspekaren – inte ens om det hinner stängas av
  // omräkningen. Ett svar hamnar därför alltid på ärendet som visades.
  const [seen, setSeen] = React.useState<{ data: typeof list.data; order: string[]; byId: Record<string, ReviewItem> }>({
    data: undefined,
    order: [],
    byId: {},
  });
  if (list.data && list.data !== seen.data) {
    const byId = { ...seen.byId };
    const order = [...seen.order];
    for (const i of list.data.items) {
      if (!(i.id in byId)) order.push(i.id);
      byId[i.id] = i;
    }
    setSeen({ data: list.data, order, byId });
  }
  const [shown, setShown] = React.useState<string | null>(null);

  const fetchedAt = list.dataUpdatedAt;
  const all = React.useMemo(() => {
    const live = new Set((seen.data?.items ?? []).map((i) => i.id));
    const hidden = (id: string) => done.has(id) && !(saved[id] !== undefined && fetchedAt > saved[id]);
    return seen.order
      .filter((id) => live.has(id) && !hidden(id))
      .map((id) => seen.byId[id])
      .filter((i) => !HIDDEN_REVIEW_KINDS.includes(i.kind));
  }, [seen, done, saved, fetchedAt]);
  const counts = React.useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of all) c[i.kind] = (c[i.kind] ?? 0) + 1;
    return c;
  }, [all]);
  const effectiveKind = kind !== "all" && !counts[kind] ? "all" : kind;
  const queue = React.useMemo(() => {
    const items = all.filter((i) => effectiveKind === "all" || i.kind === effectiveKind);
    // Ett ärende som lagts tillbaka kan ha bytts mot ett nytt för samma resa (t.ex. syfte → kategori).
    const isFront = (i: ReviewItem) => front.includes(i.id) || (!!i.trip && front.includes(i.trip.id));
    const first = items.filter(isFront);
    const rest = items.filter((i) => !isFront(i));
    const ordered = [...first, ...rest.filter((i) => !skipped.includes(i.id)), ...skipped.map((id) => rest.find((i) => i.id === id)).filter(Boolean)] as ReviewItem[];
    // Ärendet som visas ligger kvar först tills ägaren svarar eller hoppar över det.
    const pinned = shown && !done.has(shown) && !skipped.includes(shown) && !front.length ? seen.byId[shown] : undefined;
    if (!pinned || (effectiveKind !== "all" && pinned.kind !== effectiveKind)) return ordered;
    return [pinned, ...ordered.filter((i) => i.id !== pinned.id)];
  }, [all, effectiveKind, skipped, front, shown, done, seen]);
  const current = queue[0];
  if ((current?.id ?? null) !== shown) setShown(current?.id ?? null);

  /** Resans övriga ärenden där man klassar resan: de döljs med svaret och kommer tillbaka
   *  när listan har hämtats om, om de fortfarande är öppna (t.ex. syftet efter Arbete). */
  const sameTrip = (item: ReviewItem) =>
    all.filter((i) => i.id !== item.id && !!item.trip && i.trip?.id === item.trip.id && TRIP_DECISION_KINDS.has(i.kind)).map((i) => i.id);

  /** Tar ärendet (och `also`) ur kön. Returnerar false om det redan är besvarat. */
  const finish = React.useCallback((item: ReviewItem, also: string[] = []) => {
    if (answered.current.has(item.id)) return false;
    answered.current.add(item.id);
    const keys = [item.id, item.trip?.id].filter(Boolean) as string[];
    const ids = [item.id, ...also];
    setDone((d) => new Set([...d, ...ids]));
    setSaved((s) => {
      if (!ids.some((id) => id in s)) return s;
      const n = { ...s };
      for (const id of ids) delete n[id];
      return n;
    });
    setSkipped((s) => s.filter((x) => x !== item.id));
    setFront((f) => f.filter((x) => !keys.includes(x)));
    setFailed((f) => {
      if (!keys.some((k) => k in f)) return f;
      const n = { ...f };
      for (const k of keys) delete n[k];
      return n;
    });
    return true;
  }, []);

  /** Lägger tillbaka ärendet (eller resans nya ärende) först i kön – efter ett fel från
   *  servern (med felet synligt) eller vid Ångra. */
  const restore = React.useCallback((item: ReviewItem, error?: unknown, also: string[] = []) => {
    answered.current.delete(item.id);
    const keys = [item.id, item.trip?.id].filter(Boolean) as string[];
    setDone((d) => {
      const n = new Set(d);
      for (const id of [item.id, ...also]) n.delete(id);
      return n;
    });
    setFront((f) => [...keys, ...f.filter((x) => !keys.includes(x))]);
    if (error !== undefined) {
      const message = errorMessage(error);
      setFailed((f) => ({ ...f, ...Object.fromEntries(keys.map((k) => [k, message])) }));
    }
  }, []);

  /** Servern har sparat svaret på ärendet: listan som hämtas härefter avgör om det är kvar
   *  (t.ex. "Syfte saknas" efter Arbete). */
  const confirm = React.useCallback(
    (item: ReviewItem, also: string[] = []) => {
      answered.current.delete(item.id);
      const ids = [item.id, ...also];
      const at = Date.now();
      setSaved((s) => ({ ...s, ...Object.fromEntries(ids.map((id) => [id, at])) }));
      // Kommer ärendet tillbaka hamnar det sist i kön, inte direkt efter det ägaren svarar på nu.
      setSeen((s) => ({ ...s, order: [...s.order.filter((id) => !ids.includes(id)), ...ids] }));
      void qc.invalidateQueries({ queryKey: qk.review.all });
    },
    [qc],
  );

  const undoReopen = (item: ReviewItem) => ({
    label: "Ångra",
    onClick: () =>
      api
        .post(`/review/${item.id}/reopen`)
        .then(() => {
          restore(item);
          void invalidateJournalData(qc);
        })
        .catch((e) => toast.error(errorMessage(e))),
  });

  const skip = React.useCallback(() => {
    if (!current) return;
    setFront((f) => f.filter((x) => x !== current.id && x !== current.trip?.id));
    setSkipped((s) => [...s.filter((x) => x !== current.id), current.id]);
  }, [current]);

  const decideCategory = React.useCallback(
    (c: "work" | "private") => {
      const item = current;
      const also = item ? sameTrip(item) : [];
      if (!item?.trip || !finish(item, also)) return;
      actions.setCategory(item.trip.id, c, { onUndone: () => restore(item, undefined, also) }).then(
        () => confirm(item, also),
        (e) => restore(item, e, also),
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [current, all, actions, finish, restore, confirm],
  );

  const approve = React.useCallback(() => {
    const item = current;
    const also = item ? sameTrip(item) : [];
    if (!item?.trip || !hasProposal(item.trip) || !finish(item, also)) return;
    // Förslaget som visades: har det hunnit räknas om sedan dess godkänns ingenting.
    actions
      .approve(item.trip.id, {
        expected: { category: item.trip.category as CategoryCode, purpose: item.trip.purpose ?? null },
        onUndone: () => restore(item, undefined, also),
      })
      .then(
        () => confirm(item, also),
        (e) => restore(item, e, also),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, all, actions, finish, restore, confirm]);

  const savePurpose = (text: string) => {
    const item = current;
    if (!item?.trip) return;
    if (item.kind === "purpose" && !finish(item)) return;
    actions.setPurpose(item.trip.id, text).then(
      () => {
        if (item.kind === "purpose") confirm(item);
      },
      (e) => {
        if (item.kind === "purpose") restore(item, e);
      },
    );
  };

  const answerMerge = React.useCallback(
    (accept: boolean) => {
      const item = current;
      if (!item || item.kind !== "merge" || !finish(item)) return;
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
                  : undoReopen(item),
            }),
          onError: (e) => {
            restore(item, e);
            toast.error(errorMessage(e));
          },
        },
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [current, answer, finish, restore, qc],
  );

  const itemAction = (action: "dismiss" | "snooze" | "resolve") => {
    const item = current;
    if (!item || !finish(item)) return;
    reviewAction.mutate(
      { itemId: item.id, action },
      {
        onSuccess: () =>
          toast.success(action === "dismiss" ? "Ärendet är avfärdat." : action === "snooze" ? "Uppskjutet ett dygn." : "Markerat som hanterat.", {
            action: undoReopen(item),
          }),
        onError: (e) => {
          restore(item, e);
          toast.error(errorMessage(e));
        },
      },
    );
  };

  // Tangentbordsgenvägar: A, P, G, J, N, →
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Inget väntar på servern: tangenterna går att trycka i rask takt. En nedhållen tangent
      // (upprepning) besvarar däremot inte flera ärenden.
      if (e.metaKey || e.ctrlKey || e.altKey || e.repeat || isTypingTarget(e.target) || !current) return;
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
  }, [current, answerMerge, decideCategory, approve, skip]);

  const tabs = Object.keys(REVIEW_KIND_LABELS).filter((k) => counts[k]);
  const total = all.length;

  return (
    <>
      <PageHeader
        title="Granskning"
        description="Gå igenom det som behöver ditt beslut – ett ärende i taget, utan att öppna varje resa."
        actions={
          total ? (
            <>
              <Badge tone="warn" className="text-sm num">
                {formatNumber(total, 0)} att granska
              </Badge>
              <Button variant="secondary" onClick={() => setAiReview(true)} data-testid="review-ai-review">
                <Sparkles aria-hidden /> Gå igenom väntande med AI
              </Button>
            </>
          ) : null
        }
      />
      <AiReviewDialog
        open={aiReview}
        onOpenChange={setAiReview}
        selection={{ kind: "pending" }}
        trips={all.map((i) => i.trip).filter((t): t is Trip => !!t)}
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
                <Card
                  key={current.id}
                  className="animate-pop-in p-5 sm:p-6"
                  data-testid="review-current"
                  data-item-id={current.id}
                  data-trip-id={current.trip?.id}
                >
                  <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                    <Badge tone={current.kind === "merge" ? "accent" : current.kind === "category" ? "undecided" : "neutral"}>
                      {label(REVIEW_KIND_LABELS, current.kind)}
                    </Badge>
                    <span className="text-xs text-fg-muted num">
                      {queue.length > 1 ? `${formatNumber(queue.length - 1, 0)} till efter detta` : "Sista ärendet"}
                    </span>
                  </div>
                  {failed[current.id] ?? (current.trip ? failed[current.trip.id] : undefined) ? (
                    <p role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-err/40 bg-err-soft p-3 text-sm text-err-ink" data-testid="review-failed">
                      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                      <span>
                        Ditt svar kunde inte sparas: {failed[current.id] ?? failed[current.trip?.id ?? ""]} Ärendet ligger kvar – svara igen.
                      </span>
                    </p>
                  ) : null}
                  <h2 className="mb-1 text-lg font-semibold tracking-tight">{current.title}</h2>
                  {current.detail && current.kind !== "merge" ? <p className="mb-4 text-sm text-fg-muted">{current.detail}</p> : <div className="mb-4" />}

                  {current.kind === "merge" ? (
                    <MergeQuestion item={current} onAnswer={answerMerge} />
                  ) : current.trip && TRIP_DECISION_KINDS.has(current.kind) ? (
                    <TripDecision
                      key={current.trip.id}
                      item={current}
                      trip={current.trip}
                      onCategory={decideCategory}
                      onApprove={approve}
                      onPurpose={savePurpose}
                    />
                  ) : (
                    <div className="space-y-4">
                      {current.trip ? <TripSummary trip={current.trip} /> : null}
                      <Button variant="primary" size="lg" onClick={(e) => once(e, () => itemAction("resolve"))}>
                        <Check aria-hidden /> Markera som hanterat
                      </Button>
                    </div>
                  )}

                  <div className="mt-6 flex flex-wrap items-center gap-1.5 border-t border-border pt-4">
                    <Button variant="ghost" size="sm" onClick={skip} disabled={queue.length < 2}>
                      Hoppa över <ArrowRight aria-hidden />
                    </Button>
                    <Button variant="ghost" size="sm" onClick={(e) => once(e, () => itemAction("snooze"))}>
                      <Clock aria-hidden /> Skjut upp
                    </Button>
                    <Button variant="ghost" size="sm" onClick={(e) => once(e, () => itemAction("dismiss"))}>
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
              {saving ? (
                <p className="mt-3 text-xs text-fg-muted" role="status" data-testid="review-saving">
                  Sparar {formatNumber(saving, 0)} svar …
                </p>
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
