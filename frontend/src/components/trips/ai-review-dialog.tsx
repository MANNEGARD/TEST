"use client";

import * as React from "react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CircleCheckBig, Loader2, Play, ShieldCheck, Sparkles } from "lucide-react";
import { routeLabel } from "@/components/trip-detail/delete-trip-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDate, formatNumber, plural } from "@/lib/format";
import { CATEGORY_LABELS, label } from "@/lib/labels";
import { invalidateJournalData } from "@/lib/queries/keys";
import { useAiReviewPlan, useReclassify } from "@/lib/queries/trips";
import type { AiReviewPlan, ReclassifyOutcome, ReclassifyResult, Trip } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Vilka resor genomgången gäller: valda resor, eller alla som väntar i Granskning. */
export type AiReviewSelection = { kind: "trips"; ids: string[] } | { kind: "pending" };

type Outcome = ReclassifyOutcome | "error";

interface TripResult {
  id: string;
  outcome: Outcome;
  trip: Trip | null;
  message: string | null;
}

type Phase = "confirm" | "running" | "done" | "cancelled";

const OUTCOME_TEXT: Record<Outcome, string> = {
  updated: "Nytt förslag",
  unchanged: "Oförändrad",
  auto_approved: "Godkänd av dina regler",
  skipped_decided: "Redan avgjord – rördes inte",
  ai_unavailable: "AI svarade inte – reglerna användes",
  error: "Kunde inte bedömas",
};

const OUTCOME_TONE: Record<Outcome, "accent" | "neutral" | "ok" | "warn" | "err"> = {
  updated: "accent",
  unchanged: "neutral",
  auto_approved: "ok",
  skipped_decided: "neutral",
  ai_unavailable: "warn",
  error: "err",
};

/** Utfallet ur svaret. En äldre server svarar utan `outcome` – då avgör `changed`. */
function outcomeOf(res: ReclassifyResult): ReclassifyOutcome {
  return res.outcome ?? (res.changed ? "updated" : "unchanged");
}

const resor = (n: number) => plural(n, "resa", "resor");

function summaryText(results: TripResult[]): string {
  const count = (o: Outcome) => results.filter((r) => r.outcome === o).length;
  const updated = count("updated");
  const approved = count("auto_approved");
  const unchanged = count("unchanged") + count("skipped_decided");
  const failed = count("error");
  const parts = [
    `${formatNumber(updated, 0)} fick ett nytt förslag`,
    `${formatNumber(approved, 0)} godkändes av dina regler`,
    `${formatNumber(unchanged, 0)} ${unchanged === 1 ? "oförändrad" : "oförändrade"}`,
  ];
  if (failed) parts.push(`${formatNumber(failed, 0)} kunde inte bedömas`);
  return `${parts.slice(0, -1).join(", ")} och ${parts[parts.length - 1]}.`;
}

/**
 * AI-genomgång (byggkontraktet 6.D–6.E): bedömer resorna en i taget med POST /trips/{id}/classify,
 * styrt härifrån, så att förloppet syns och genomgången kan avbrytas efter pågående resa. Resor som
 * redan är godkända eller importerade rörs aldrig – planen tar inte med dem och servern vägrar.
 * Utan AI-leverantör körs bara reglerna, och ingenting lämnar servern.
 */
export function AiReviewDialog({
  open,
  onOpenChange,
  selection,
  trips = [],
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  selection: AiReviewSelection;
  /** Resor som webbappen redan känner till – ger rutten i förloppet och i resultatet. */
  trips?: Trip[];
}) {
  const [busy, setBusy] = React.useState(false);
  return (
    <Dialog open={open} onOpenChange={(o) => (busy && !o ? undefined : onOpenChange(o))}>
      <DialogContent className="max-w-lg" data-testid="ai-review-dialog" hideClose={busy}>
        {open ? <ReviewBody selection={selection} trips={trips} onClose={() => onOpenChange(false)} onBusy={setBusy} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function ReviewBody({
  selection,
  trips,
  onClose,
  onBusy,
}: {
  selection: AiReviewSelection;
  trips: Trip[];
  onClose: () => void;
  onBusy: (busy: boolean) => void;
}) {
  const qc = useQueryClient();
  const plan = useAiReviewPlan();
  const classify = useReclassify({ invalidate: false });
  const [phase, setPhase] = React.useState<Phase>("confirm");
  const [results, setResults] = React.useState<TripResult[]>([]);
  const [currentId, setCurrentId] = React.useState<string | null>(null);
  const [stopping, setStopping] = React.useState(false);
  const stopRef = React.useRef(false);
  const known = React.useMemo(() => new Map(trips.map((t) => [t.id, t])), [trips]);

  const requestPlan = plan.mutate;
  const body = selection.kind === "pending" ? { trip_ids: null, pending: true } : { trip_ids: selection.ids, pending: false };
  const bodyKey = JSON.stringify(body);
  React.useEffect(() => {
    requestPlan(JSON.parse(bodyKey));
  }, [requestPlan, bodyKey]);

  // Stängs vyn mitt i genomgången avbryts den efter pågående resa.
  React.useEffect(
    () => () => {
      stopRef.current = true;
    },
    [],
  );

  const start = async (p: AiReviewPlan) => {
    stopRef.current = false;
    setStopping(false);
    setResults([]);
    setPhase("running");
    onBusy(true);
    const done: TripResult[] = [];
    for (const id of p.trip_ids) {
      if (stopRef.current) break;
      setCurrentId(id);
      try {
        const res = await classify.mutateAsync(id);
        done.push({ id, outcome: outcomeOf(res), trip: res.trip ?? null, message: res.note });
      } catch (err) {
        done.push({ id, outcome: "error", trip: null, message: errorMessage(err) });
      }
      setResults([...done]);
    }
    setCurrentId(null);
    onBusy(false);
    setPhase(done.length < p.trip_ids.length ? "cancelled" : "done");
    void invalidateJournalData(qc);
  };

  const stop = () => {
    stopRef.current = true;
    setStopping(true);
  };

  if (plan.isError) {
    return (
      <>
        <DialogHeader>
          <DialogTitle>Gå igenom med AI</DialogTitle>
          <DialogDescription className="sr-only">Planen för genomgången kunde inte hämtas.</DialogDescription>
        </DialogHeader>
        <ErrorState compact error={plan.error} onRetry={() => requestPlan(JSON.parse(bodyKey))} title="Det gick inte att räkna resorna" />
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Stäng
          </Button>
        </DialogFooter>
      </>
    );
  }

  const p = plan.data;
  if (!p) {
    return (
      <>
        <DialogHeader>
          <DialogTitle>Gå igenom med AI</DialogTitle>
          <DialogDescription>Räknar resor …</DialogDescription>
        </DialogHeader>
        <div className="space-y-2" role="status" aria-label="Räknar resor">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      </>
    );
  }

  const n = p.trip_ids.length;
  const nameOf = (id: string, after?: Trip | null) => {
    const t = after ?? known.get(id);
    return t ? routeLabel(t) : "Resa";
  };

  if (phase === "confirm" && n === 0) {
    return (
      <>
        <DialogHeader>
          <DialogTitle>Gå igenom med AI</DialogTitle>
          <DialogDescription data-testid="ai-review-empty">
            Det finns inga resor att gå igenom. Resor som redan är godkända eller importerade rörs inte.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="primary" onClick={onClose}>
            Stäng
          </Button>
        </DialogFooter>
      </>
    );
  }

  if (phase === "confirm") {
    const skipped = p.skipped_decided;
    return (
      <>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="size-4 text-accent-ink" aria-hidden /> Gå igenom med AI
          </DialogTitle>
          <DialogDescription data-testid="ai-review-intro">
            {p.provider ? (
              <>
                {resor(n)} skickas till {p.provider_label ?? p.provider} för bedömning, en i taget.
              </>
            ) : (
              <>
                Ingen AI-leverantör är vald, så bara reglerna och dina tidigare val används. Ingenting skickas utanför servern. {resor(n)} bedöms.
              </>
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2.5 text-sm leading-relaxed text-fg">
          {p.provider ? (
            <p className="text-fg-muted">
              {p.context_level === "full"
                ? "Med fullständigt underlag skickas bland annat platsernas namn och adresser, dagens resor och kalenderbokningar (se Inställningar → Förslag och AI)."
                : "Med minimalt underlag skickas bara platsens namn, ort, tid och sträcka."}
            </p>
          ) : (
            <p>
              <Link
                href="/integrations"
                className="inline-flex items-center gap-1 font-medium text-accent-ink underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
              >
                Välj leverantör under Integrationer
                <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            </p>
          )}
          <p>
            Resor som redan är godkända eller importerade rörs inte
            {skipped ? ` (${formatNumber(skipped, 0)} hoppas över)` : ""}.
          </p>
          {p.total_eligible > p.cap ? (
            <p className="rounded-md bg-surface-2 px-3 py-2 text-[13px] text-fg-muted" data-testid="ai-review-cap">
              De {formatNumber(p.cap, 0)} äldsta tas nu. Kör igen för resten.
            </p>
          ) : null}
          <p className="flex items-start gap-2 rounded-lg border border-border bg-surface-2/50 p-3 text-[13px] text-fg-muted">
            <ShieldCheck className="mt-0.5 size-4 shrink-0 text-ok-ink" aria-hidden />
            <span>
              AI godkänner aldrig något själv – du får förslag att godkänna. I självgående läge kan dina regler och platsantagandet (med vetotid) godkänna resor
              som vanligt.
            </span>
          </p>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Avbryt
          </Button>
          <Button variant="primary" onClick={() => void start(p)} data-testid="ai-review-start">
            <Play aria-hidden /> Starta
          </Button>
        </DialogFooter>
      </>
    );
  }

  if (phase === "running") {
    const i = Math.min(results.length + 1, n);
    return (
      <>
        <DialogHeader>
          <DialogTitle>Gå igenom med AI</DialogTitle>
          <DialogDescription role="status" aria-live="polite" data-testid="ai-review-progress">
            Bedömer {formatNumber(i, 0)} av {formatNumber(n, 0)} …
          </DialogDescription>
        </DialogHeader>
        <div
          className="h-1.5 overflow-hidden rounded-full bg-surface-3"
          role="progressbar"
          aria-label="Genomgångens förlopp"
          aria-valuemin={0}
          aria-valuemax={n}
          aria-valuenow={results.length}
        >
          <div className="h-full rounded-full bg-accent transition-[width] duration-200" style={{ width: `${Math.max(4, Math.round((results.length / n) * 100))}%` }} />
        </div>
        {currentId ? (
          <p className="flex items-center gap-2 text-[13px] text-fg-muted">
            <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden />
            <span className="min-w-0 truncate">{nameOf(currentId)}</span>
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={stop} disabled={stopping} data-testid="ai-review-stop">
            {stopping ? "Avbryts efter den här resan" : "Avbryt"}
          </Button>
        </DialogFooter>
      </>
    );
  }

  const unavailable = results.filter((r) => r.outcome === "ai_unavailable").length;
  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <CircleCheckBig className="size-4 text-ok-ink" aria-hidden /> {phase === "cancelled" ? "Genomgången avbröts" : "Genomgången är klar"}
        </DialogTitle>
        <DialogDescription role="status" data-testid="ai-review-summary">
          {phase === "cancelled" ? `Avbruten efter ${formatNumber(results.length, 0)} av ${formatNumber(n, 0)}. ` : null}
          {phase === "cancelled" ? summaryText(results) : `Klart: ${summaryText(results)}`}
        </DialogDescription>
      </DialogHeader>
      {unavailable ? (
        <p className="rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-[13px] text-warn-ink">
          AI-leverantören svarade inte – reglerna användes för {resor(unavailable)}.
        </p>
      ) : null}
      {results.length ? (
        <ul className="scrollbar-thin max-h-72 divide-y divide-border overflow-y-auto rounded-lg border border-border" aria-label="Resultat per resa">
          {results.map((r) => {
            const t = r.trip ?? known.get(r.id) ?? null;
            const when = t?.started_at ?? t?.sort_time;
            return (
              <li key={r.id} className="px-3 py-2.5 text-[13px]">
                <div className="flex items-start justify-between gap-3">
                  <Link
                    href={`/journal/${r.id}`}
                    className="min-w-0 font-medium text-fg underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                    onClick={onClose}
                  >
                    {when ? <span className="num mr-1.5 font-normal text-fg-muted">{formatDate(when)}</span> : null}
                    {nameOf(r.id, r.trip)}
                  </Link>
                  <Badge tone={OUTCOME_TONE[r.outcome]} size="sm" className="shrink-0">
                    {OUTCOME_TEXT[r.outcome]}
                  </Badge>
                </div>
                {r.trip && (r.outcome === "updated" || r.outcome === "auto_approved" || r.outcome === "ai_unavailable") ? (
                  <p className={cn("mt-0.5 text-fg-muted", r.trip.category === "work" && "text-fg")}>
                    {label(CATEGORY_LABELS, r.trip.category)}
                    {r.trip.purpose ? ` – ${r.trip.purpose}` : ""}
                  </p>
                ) : r.outcome === "error" && r.message ? (
                  <p className="mt-0.5 text-err-ink">{r.message}</p>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      <DialogFooter>
        <Button variant="primary" onClick={onClose} data-testid="ai-review-close">
          Stäng
        </Button>
      </DialogFooter>
    </>
  );
}
