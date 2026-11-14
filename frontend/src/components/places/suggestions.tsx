"use client";

import * as React from "react";
import { ArrowRight, Check, Merge, Sparkles, X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { errorMessage } from "@/lib/api";
import { formatNumber, plural } from "@/lib/format";
import type { MergeSuggestion, Place, PlaceInput } from "@/lib/places-types";
import { useAnswerSuggestion, useMergePlaces, usePlaceSuggestions, useUpdatePlace } from "@/lib/queries/places";
import { cn, readStorage, writeStorage } from "@/lib/utils";
import { LocationTypeIcon, PlaceAvatar, typeLabel } from "./location-type";

const DISMISSED_KEY = "drivelog.places.dismissed-merges";
const INITIAL_ROWS = 5;

function mergeKey(m: MergeSuggestion): string {
  return m.locations
    .map((l) => l.id)
    .sort()
    .join("+");
}

function reasonText(reason: string | null): string {
  // Backend inleder med "Förslag: <typ>." – typen visas redan som märke.
  return (reason ?? "").replace(/^Förslag:\s*[^.]+\.\s*/, "");
}

/**
 * Förslag överst i platslistan: föreslagen typ/namn (godkänn eller avvisa med ett
 * klick, båda kan ångras) och grannadresser som kan vara samma plats (slå ihop).
 * Visas bara när det finns något att ta ställning till.
 */
export function Suggestions({ onOpenPlace }: { onOpenPlace: (id: string) => void }) {
  const suggestions = usePlaceSuggestions();
  const answer = useAnswerSuggestion();
  const update = useUpdatePlace();
  const [dismissed, setDismissed] = React.useState<string[]>(() => (typeof window === "undefined" ? [] : readStorage<string[]>("local", DISMISSED_KEY, [])));
  const [showAll, setShowAll] = React.useState(false);

  if (suggestions.isError) {
    return (
      <Card className="mb-5">
        <ErrorState compact title="Det gick inte att hämta förslagen" error={suggestions.error} onRetry={() => suggestions.refetch()} />
      </Card>
    );
  }
  const types = suggestions.data?.types ?? [];
  const merges = (suggestions.data?.merges ?? []).filter((m) => !dismissed.includes(mergeKey(m)));
  const total = types.length + merges.length;
  if (!total) return null;

  // mutateAsync + then: kvittot ska visas även om raden hinner försvinna ur listan.
  const undo = (id: string, patch: PlaceInput) => ({
    label: "Ångra",
    onClick: () =>
      void update.mutateAsync({ id, patch }).then(
        () => toast.success("Ändringen är ångrad."),
        (e) => toast.error(errorMessage(e)),
      ),
  });

  const respond = (place: Place, accept: boolean) =>
    void answer.mutateAsync({ id: place.id, accept }).then(
      (saved) =>
        accept
          ? toast.success(`${saved.name} är sparad som ${typeLabel(saved.type).toLowerCase()}.`, {
              action: undo(place.id, { name: place.name, type: place.type, company: place.company ?? "", confirmed: false }),
            })
          : toast.success("Förslaget är avvisat.", { action: undo(place.id, { confirmed: false }) }),
      (e) => toast.error(errorMessage(e)),
    );

  const dismissMerge = (m: MergeSuggestion) => {
    const next = [...dismissed, mergeKey(m)];
    setDismissed(next);
    writeStorage("local", DISMISSED_KEY, next);
  };

  const visibleTypes = showAll ? types : types.slice(0, INITIAL_ROWS);
  const pendingId = answer.isPending ? answer.variables?.id : undefined;

  return (
    <Card className="mb-5 overflow-hidden" data-testid="place-suggestions">
      <div className="px-4 pt-4 pb-3 sm:px-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold tracking-tight">
          <Sparkles className="size-4 text-accent" aria-hidden />
          Förslag
          <Badge tone="accent" size="sm" className="num">
            {formatNumber(total, 0)}
          </Badge>
        </h2>
        <p className="mt-0.5 text-xs text-fg-muted">Gissningar utifrån dina resor. Inget ändras förrän du godkänner.</p>
      </div>
      <ul className="divide-y divide-border border-t border-border">
        {visibleTypes.map((p) => (
          <li key={p.id} className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:px-5" data-testid="type-suggestion">
            <div className="flex min-w-0 flex-1 items-start gap-3">
              <PlaceAvatar type={p.suggested_type ?? "unknown"} className="mt-0.5" />
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                  <button
                    type="button"
                    onClick={() => onOpenPlace(p.id)}
                    className="min-w-0 truncate rounded-sm text-left font-medium underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                    title="Öppna platsen"
                  >
                    {p.name}
                  </button>
                  <ArrowRight className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
                  <span className="sr-only">föreslås bli</span>
                  <Badge tone="accent">
                    <LocationTypeIcon type={p.suggested_type ?? "unknown"} />
                    {typeLabel(p.suggested_type)}
                    {p.suggested_name ? ` · ${p.suggested_name}` : ""}
                  </Badge>
                </p>
                <p className="mt-1 text-xs leading-relaxed text-fg-muted">{reasonText(p.suggestion_reason)}</p>
              </div>
            </div>
            <div className="grid shrink-0 grid-cols-2 gap-2 sm:flex">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => respond(p, false)}
                disabled={answer.isPending}
                aria-label={`Avvisa förslaget för ${p.name}`}
                data-testid="suggestion-reject"
              >
                <X aria-hidden /> Avvisa
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => respond(p, true)}
                disabled={answer.isPending && pendingId !== p.id}
                loading={pendingId === p.id && answer.variables?.accept}
                aria-label={`Godkänn förslaget för ${p.name}`}
                data-testid="suggestion-accept"
              >
                {pendingId === p.id && answer.variables?.accept ? null : <Check aria-hidden />} Godkänn
              </Button>
            </div>
          </li>
        ))}
        {types.length > INITIAL_ROWS && !showAll ? (
          <li className="px-4 py-2 sm:px-5">
            <Button variant="ghost" size="sm" className="-ml-2" onClick={() => setShowAll(true)}>
              Visa {formatNumber(types.length - INITIAL_ROWS, 0)} förslag till
            </Button>
          </li>
        ) : null}
        {merges.map((m) => (
          <MergeRow key={mergeKey(m)} suggestion={m} onDismiss={() => dismissMerge(m)} />
        ))}
      </ul>
    </Card>
  );
}

function MergeRow({ suggestion, onDismiss }: { suggestion: MergeSuggestion; onDismiss: () => void }) {
  const merge = useMergePlaces();
  const [keep, setKeep] = React.useState(suggestion.locations[0]?.id ?? "");
  const group = React.useId();
  const keepPlace = suggestion.locations.find((l) => l.id === keep) ?? suggestion.locations[0];

  // Raden försvinner när listan hämtats om – därför mutateAsync + then för kvittot.
  const run = () =>
    void merge.mutateAsync({ keepId: keepPlace.id, removeIds: suggestion.locations.filter((l) => l.id !== keepPlace.id).map((l) => l.id) }).then(
      () => toast.success(`Platserna är ihopslagna till ${keepPlace.name}.`),
      (e) => toast.error(errorMessage(e)),
    );

  return (
    <li className="flex flex-col gap-3 px-4 py-3.5 sm:px-5 lg:flex-row lg:items-start" data-testid="merge-suggestion">
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <span aria-hidden className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-full bg-accent-soft text-accent-ink">
          <Merge className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">Samma plats?</p>
          <p className="mt-0.5 text-xs leading-relaxed text-fg-muted">{suggestion.reason}</p>
          <fieldset className="mt-2.5 space-y-1.5">
            <legend className="mb-1.5 text-xs font-medium text-fg-muted">Välj vilken som ska behållas</legend>
            {suggestion.locations.map((l) => (
              <label
                key={l.id}
                className={cn(
                  "flex max-w-xl cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 text-sm transition-colors",
                  keepPlace.id === l.id ? "border-accent bg-accent-soft/50" : "border-border hover:border-border-strong",
                )}
              >
                <input type="radio" name={group} className="size-4 shrink-0 accent-accent" checked={keepPlace.id === l.id} onChange={() => setKeep(l.id)} />
                <span className="min-w-0 flex-1 truncate">{l.name}</span>
                <span className="shrink-0 text-xs text-fg-muted num">{plural(l.visit_count, "besök", "besök")}</span>
              </label>
            ))}
          </fieldset>
        </div>
      </div>
      <div className="flex shrink-0 flex-col gap-1.5 lg:items-end">
        <div className="grid grid-cols-2 gap-2 sm:flex">
          <Button size="sm" variant="ghost" onClick={onDismiss} disabled={merge.isPending}>
            <X aria-hidden /> Inte samma
          </Button>
          <Button size="sm" variant="primary" onClick={run} loading={merge.isPending} data-testid="merge-accept">
            {merge.isPending ? null : <Merge aria-hidden />} Slå ihop
          </Button>
        </div>
        <p className="text-xs text-fg-muted">Går inte att ångra.</p>
      </div>
    </li>
  );
}
