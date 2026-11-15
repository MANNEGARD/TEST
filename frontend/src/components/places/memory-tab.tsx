"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, Ban, Brain, CircleHelp, Plus, RotateCcw, Trash2, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { CategoryPill } from "@/components/trips/category-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDate, formatNumber, formatRelative, plural } from "@/lib/format";
import type { Pattern, PatternStatus, Place } from "@/lib/places-types";
import { useCorrections, useCreateRule, useDeleteRule, usePatterns, useSetPatternStatus, useTripsById, useUndoCorrection } from "@/lib/queries/places";
import type { Feedback, Trip } from "@/lib/types";
import { RuleDialog, type RulePreset } from "./rule-dialog";
import { useReturnFocus } from "./use-return-focus";

const INITIAL_PATTERNS = 6;

function patternTitle(p: Pattern): React.ReactNode {
  const destination = p.destination_name ?? "Okänd plats";
  if (p.pattern_type !== "route") return destination;
  return (
    <>
      {p.origin_name ?? "Okänd plats"} <ArrowRight className="mx-0.5 inline size-3.5 text-fg-subtle" aria-label="till" /> {destination}
    </>
  );
}

function patternName(p: Pattern): string {
  const destination = p.destination_name ?? "okänd plats";
  return p.pattern_type === "route" ? `${p.origin_name ?? "okänd plats"} till ${destination}` : destination;
}

function SectionHeader({ title, description, action }: { title: string; description?: string; action?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 px-4 pt-4 pb-3 sm:px-5">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {description ? <p className="mt-0.5 text-xs text-fg-muted">{description}</p> : null}
      </div>
      {action}
    </div>
  );
}

/**
 * Fliken "AI-minne": vad som lärts in per plats/rutt, egna regler och de senaste
 * korrigeringarna (som kan ångras).
 */
export function MemoryTab({ places }: { places: Place[] }) {
  const patterns = usePatterns();
  const [rule, setRule] = React.useState<{ open: boolean; preset?: RulePreset }>({ open: false });
  const focus = useReturnFocus();
  const openRule = (preset?: RulePreset) => {
    focus.remember();
    setRule({ open: true, preset });
  };

  return (
    <>
      <p className="mb-4 flex max-w-3xl items-start gap-2 text-sm text-fg-muted">
        <Brain className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
        Här ser du vad DriveLog har lärt sig av dina resor och ändringar. Det används för att föreslå kategori och syfte på nya resor – du bestämmer alltid själv.
      </p>
      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
        <PatternsCard query={patterns} onRule={openRule} />
        <div className="space-y-5">
          <RulesCard query={patterns} onNew={() => openRule()} canCreate={places.length > 0} />
          <CorrectionsCard />
        </div>
      </div>
      <RuleDialog open={rule.open} onOpenChange={(open) => setRule((r) => ({ ...r, open }))} places={places} preset={rule.preset} onCloseAutoFocus={focus.restore} />
    </>
  );
}

// ----------------------------------------------------------------------------- inlärda mönster

function PatternsCard({ query, onRule }: { query: ReturnType<typeof usePatterns>; onRule: (preset: RulePreset) => void }) {
  const [kind, setKind] = React.useState<"destination" | "route">("destination");
  const [showAll, setShowAll] = React.useState(false);
  const all = React.useMemo(() => (query.data ?? []).filter((p) => p.confirmed_work + p.confirmed_private + p.imported_work + p.imported_private > 0), [query.data]);
  // Avvisade mönster sist (stabil sortering behåller backendens ordning i övrigt).
  const rows = all
    .filter((p) => (kind === "route" ? p.pattern_type === "route" : p.pattern_type !== "route"))
    .sort((a, b) => Number(a.status === "rejected") - Number(b.status === "rejected"));
  const active = rows.filter((p) => p.status !== "rejected").length;
  // Avvisade mönster visas bara under "Visa alla".
  const visible = showAll ? rows : rows.slice(0, Math.min(INITIAL_PATTERNS, active));
  const setStatus = useSetPatternStatus();
  const changeStatus = (p: Pattern, status: PatternStatus) => {
    const before = p.status as PatternStatus;
    const name = patternName(p);
    void setStatus.mutateAsync({ id: p.id, status }).then(
      () =>
        toast.success(status === "rejected" ? `Mönstret för ${name} används inte längre.` : `Mönstret för ${name} används igen.`, {
          action: {
            label: "Ångra",
            onClick: () =>
              void setStatus.mutateAsync({ id: p.id, status: before }).then(
                () => toast.success("Ändringen är ångrad."),
                (e) => toast.error(errorMessage(e)),
              ),
          },
        }),
      (e) => toast.error(errorMessage(e)),
    );
  };

  return (
    <Card className="overflow-hidden" data-testid="patterns-card">
      <SectionHeader
        title="Inlärda mönster"
        description="Hur resor till dina platser brukar klassas."
        action={
          all.length ? (
            <SegmentedControl<"destination" | "route">
              ariaLabel="Visa mönster per plats eller rutt"
              value={kind}
              onChange={(v) => {
                setKind(v);
                setShowAll(false);
              }}
              options={[
                { value: "destination", label: "Per plats" },
                { value: "route", label: "Per rutt" },
              ]}
            />
          ) : null
        }
      />
      {query.isError ? (
        <ErrorState compact error={query.error} onRetry={() => query.refetch()} />
      ) : !query.data ? (
        <SkeletonBlock rows={6} className="px-5 pb-5" />
      ) : !all.length ? (
        <EmptyState
          compact
          icon={<Brain />}
          title="Inget inlärt ännu"
          description="När du har importerat eller godkänt några resor ser du här hur resor till dina platser brukar klassas."
          action={
            <Button asChild>
              <Link href="/journal">Till körjournalen</Link>
            </Button>
          }
        />
      ) : !rows.length ? (
        <p className="border-t border-border px-5 py-6 text-center text-sm text-fg-muted">
          {kind === "route" ? "Inga rutter har körts tillräckligt ofta ännu." : "Inga platser har besökts tillräckligt ofta ännu."}
        </p>
      ) : (
        <>
          <ul className="divide-y divide-border border-t border-border">
            {visible.map((p) => (
              <PatternRow
                key={p.id}
                pattern={p}
                onRule={onRule}
                onStatus={(status) => changeStatus(p, status)}
                busy={setStatus.isPending && setStatus.variables?.id === p.id}
              />
            ))}
          </ul>
          {rows.length > visible.length || showAll ? (
            <div className="border-t border-border px-4 py-2 sm:px-5">
              <Button variant="ghost" size="sm" className="-ml-2" onClick={() => setShowAll(!showAll)} aria-expanded={showAll}>
                {showAll
                  ? "Visa färre"
                  : `Visa alla ${formatNumber(rows.length, 0)}${rows.length > active ? ` (${formatNumber(rows.length - active, 0)} används inte)` : ""}`}
              </Button>
            </div>
          ) : null}
        </>
      )}
    </Card>
  );
}

function PatternRow({
  pattern: p,
  onRule,
  onStatus,
  busy,
}: {
  pattern: Pattern;
  onRule: (preset: RulePreset) => void;
  onStatus: (status: PatternStatus) => void;
  busy: boolean;
}) {
  const rejected = p.status === "rejected";
  const work = p.confirmed_work + p.imported_work;
  const priv = p.confirmed_private + p.imported_private;
  const confirmed = p.confirmed_work + p.confirmed_private;
  const imported = p.imported_work + p.imported_private;
  const corrections = p.corrections_to_work + p.corrections_to_private;
  const facts = [
    confirmed ? `${formatNumber(confirmed, 0)} ${confirmed === 1 ? "bekräftad" : "bekräftade"} av dig` : null,
    imported ? `${formatNumber(imported, 0)} ur importerad körjournal` : null,
    corrections ? plural(corrections, "korrigering", "korrigeringar") : null,
    p.last_seen_at ? `senast ${formatDate(p.last_seen_at)}` : null,
  ].filter(Boolean);
  const purposes = p.purposes.slice(0, 3);

  return (
    <li className="flex flex-col gap-2 px-4 py-3.5 sm:flex-row sm:items-start sm:gap-4 sm:px-5" data-testid="pattern-row" data-status={p.status}>
      <div className={rejected ? "min-w-0 flex-1 opacity-60" : "min-w-0 flex-1"}>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium break-words">
          <span className="min-w-0">{patternTitle(p)}</span>
          {rejected ? (
            <Badge tone="neutral" size="sm">
              Används inte
            </Badge>
          ) : p.status === "approved" && !p.is_manual_rule ? (
            <Badge tone="ok" size="sm">
              Godkänt
            </Badge>
          ) : null}
        </p>
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted num">
          <Badge tone={work ? "work" : "outline"} size="sm">
            Arbete {formatNumber(work, 0)}
          </Badge>
          <Badge tone={priv ? "private" : "outline"} size="sm">
            Privat {formatNumber(priv, 0)}
          </Badge>
          <span>{facts.join(" · ")}</span>
        </p>
        {purposes.length ? (
          <p className="mt-1.5 text-xs leading-relaxed text-fg-muted">
            <span className="text-fg-subtle">Vanliga syften: </span>
            {purposes.map((x) => x.text).join(" · ")}
            {p.purposes.length > purposes.length ? <span className="text-fg-subtle">{`\u00a0+${p.purposes.length - purposes.length}`}</span> : null}
          </p>
        ) : null}
      </div>
      <div className="-ml-2 flex shrink-0 items-center gap-1 self-start sm:ml-0">
        {p.is_manual_rule ? (
          <Badge tone="accent">Har regel</Badge>
        ) : rejected ? (
          <Button size="xs" variant="ghost" onClick={() => onStatus("approved")} disabled={busy} aria-label={`Använd mönstret för ${patternName(p)} igen`}>
            <RotateCcw aria-hidden /> Använd igen
          </Button>
        ) : (
          <>
            <Button
              size="xs"
              variant="ghost"
              onClick={() =>
                onRule({
                  destinationId: p.destination_location_id,
                  originId: p.pattern_type === "route" ? p.origin_location_id : null,
                  category: priv > work ? "private" : "work",
                })
              }
              aria-label={`Skapa regel för ${patternName(p)}`}
            >
              <Plus aria-hidden /> Regel
            </Button>
            <Button
              size="xs"
              variant="ghost"
              className="text-fg-muted"
              onClick={() => onStatus("rejected")}
              disabled={busy}
              title="Mönstret används inte längre för att föreslå kategori och syfte"
              aria-label={`Använd inte mönstret för ${patternName(p)}`}
            >
              <Ban aria-hidden /> Använd inte
            </Button>
          </>
        )}
      </div>
    </li>
  );
}

// ----------------------------------------------------------------------------- regler

function RulesCard({ query, onNew, canCreate }: { query: ReturnType<typeof usePatterns>; onNew: () => void; canCreate: boolean }) {
  const remove = useDeleteRule();
  const create = useCreateRule();
  const rules = (query.data ?? []).filter((p) => p.is_manual_rule);

  // mutateAsync + then: kvittot (och Ångra) ska fungera även om användaren byter flik.
  const removeRule = (p: Pattern) =>
    void remove.mutateAsync(p.id).then(
      () =>
        toast.success("Regeln är borttagen.", {
          action: {
            label: "Ångra",
            onClick: () =>
              void create
                .mutateAsync({
                  destination_location_id: p.destination_location_id,
                  origin_location_id: p.pattern_type === "route" ? p.origin_location_id : null,
                  category:
                    p.manual_category === "work" || p.manual_category === "private" || p.manual_category === "undecided" ? p.manual_category : null,
                  purpose: p.manual_purpose,
                })
                .then(
                  () => toast.success("Regeln är tillbaka."),
                  (e) => toast.error(errorMessage(e)),
                ),
          },
        }),
      (e) => toast.error(errorMessage(e)),
    );

  return (
    <Card className="overflow-hidden" data-testid="rules-card">
      <SectionHeader
        title="Regler"
        description="Dina egna regler gäller före det inlärda."
        action={
          <Button size="sm" onClick={onNew} disabled={!canCreate} data-testid="new-rule">
            <Plus aria-hidden /> Ny regel
          </Button>
        }
      />
      {query.isError ? (
        <ErrorState compact error={query.error} onRetry={() => query.refetch()} />
      ) : !query.data ? (
        <SkeletonBlock rows={2} className="px-5 pb-5" />
      ) : !rules.length ? (
        <p className="border-t border-border px-4 py-5 text-sm leading-relaxed text-fg-muted sm:px-5">
          Inga regler ännu. Skapa en om resor till en plats alltid ska klassas likadant – t.ex. att resor till en kund alltid är arbete.
        </p>
      ) : (
        <ul className="divide-y divide-border border-t border-border">
          {rules.map((p) => (
            <li key={p.id} className="flex items-start gap-3 px-4 py-3 sm:px-5" data-testid="rule-row">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium break-words">{p.pattern_type === "route" ? patternTitle(p) : <>Till {p.destination_name ?? "okänd plats"}</>}</p>
                <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
                  {p.manual_category === "undecided" ? (
                    <Badge tone="undecided" size="sm">
                      <CircleHelp aria-hidden /> Fråga mig alltid
                    </Badge>
                  ) : p.manual_category ? (
                    <>
                      alltid <CategoryPill category={p.manual_category} size="sm" />
                    </>
                  ) : null}
                  {p.manual_purpose ? (
                    <span className="min-w-0 basis-full break-words">
                      Syfte: <span className="text-fg">{p.manual_purpose}</span>
                    </span>
                  ) : null}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={() => removeRule(p)}
                disabled={remove.isPending}
                aria-label={`Ta bort regeln för ${patternName(p)}`}
                className="-mr-2 shrink-0"
              >
                <Trash2 aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ----------------------------------------------------------------------------- korrigeringar

function tripSummary(t: Trip): string {
  const short = (address: string | null) => (address ? address.split(",").pop()?.trim() || address : "okänd adress");
  return `${formatDate(t.started_at ?? t.sort_time)} · ${short(t.start_address)} → ${short(t.end_address)}`;
}

function CorrectionsCard() {
  const corrections = useCorrections(8);
  const undo = useUndoCorrection();
  const items = corrections.data ?? [];
  const trips = useTripsById(items.map((f) => f.logical_trip_id));

  const undoOne = (f: Feedback) =>
    void undo.mutateAsync(f.id).then(
      () => toast.success("Korrigeringen är ångrad – resan är återställd."),
      (e) => toast.error(errorMessage(e)),
    );

  return (
    <Card className="overflow-hidden" data-testid="corrections-card">
      <SectionHeader title="Senaste korrigeringar" description="Ändringar du gjort av föreslagen kategori eller syfte." />
      {corrections.isError ? (
        <ErrorState compact error={corrections.error} onRetry={() => corrections.refetch()} />
      ) : !corrections.data ? (
        <SkeletonBlock rows={3} className="px-5 pb-5" />
      ) : !items.length ? (
        <p className="border-t border-border px-4 py-5 text-sm leading-relaxed text-fg-muted sm:px-5">
          Inga korrigeringar ännu. När du ändrar en kategori eller ett syfte sparas det här, så att förslagen blir bättre nästa gång.
        </p>
      ) : (
        <ul className="divide-y divide-border border-t border-border">
          {items.map((f) => {
            const trip = trips.get(f.logical_trip_id);
            const categoryChanged = !!f.new_category && f.new_category !== f.original_category;
            const purposeChanged = !!f.new_purpose && f.new_purpose !== f.original_purpose;
            return (
              <li key={f.id} className="flex items-start gap-3 px-4 py-3 sm:px-5" data-testid="correction-row">
                <div className="min-w-0 flex-1 space-y-1.5">
                  {categoryChanged ? (
                    <p className="flex flex-wrap items-center gap-1.5">
                      {f.original_category ? <CategoryPill category={f.original_category} size="sm" /> : null}
                      <ArrowRight className="size-3.5 text-fg-subtle" aria-label="ändrad till" />
                      <CategoryPill category={f.new_category as string} size="sm" />
                    </p>
                  ) : null}
                  {purposeChanged ? (
                    <p className="text-[13px] break-words">
                      <span className="text-fg-muted">Syfte: </span>
                      {f.original_purpose ? (
                        <>
                          <s className="text-fg-muted">{f.original_purpose}</s> <ArrowRight className="inline size-3 text-fg-subtle" aria-label="ändrat till" />{" "}
                        </>
                      ) : null}
                      {f.new_purpose}
                    </p>
                  ) : null}
                  {!categoryChanged && !purposeChanged ? <p className="text-[13px]">Ändrad klassning</p> : null}
                  <p className="text-xs text-fg-muted">
                    <Link
                      href={`/journal/${f.logical_trip_id}`}
                      className="rounded-sm underline-offset-4 hover:text-fg hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                    >
                      {trip ? tripSummary(trip) : "Visa resan"}
                    </Link>
                    <span className="mt-0.5 block text-fg-subtle">Ändrad {formatRelative(f.created_at)}</span>
                  </p>
                </div>
                {f.undone_at ? (
                  <Badge tone="neutral" className="shrink-0">
                    Ångrad
                  </Badge>
                ) : (
                  <Button
                    size="sm"
                    className="shrink-0"
                    onClick={() => undoOne(f)}
                    disabled={undo.isPending}
                    loading={undo.isPending && undo.variables === f.id}
                    aria-label={`Ångra korrigeringen${trip ? ` för resan ${tripSummary(trip)}` : ""}`}
                    data-testid="correction-undo"
                  >
                    {undo.isPending && undo.variables === f.id ? null : <Undo2 aria-hidden />} Ångra
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
