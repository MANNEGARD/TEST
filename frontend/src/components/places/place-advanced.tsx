"use client";

import * as React from "react";
import { ChevronRight, Merge, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import type { Place } from "@/lib/places-types";
import { useAddAlias, useDeletePlace, useMergePlaces } from "@/lib/queries/places";
import { cn } from "@/lib/utils";

export type AdvancedSection = "alias" | "merge" | "delete";

/** Hopfällbar rad i sidopanelen (styrd <details>, tangentbordsvänlig). */
function Fold({
  title,
  meta,
  open,
  onOpenChange,
  children,
  id,
}: {
  title: string;
  meta?: React.ReactNode;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
  id: string;
}) {
  return (
    <details
      id={id}
      open={open}
      onToggle={(e) => {
        if (e.currentTarget.open !== open) onOpenChange(e.currentTarget.open);
      }}
      className="group border-b border-border last:border-b-0"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-sm py-3 text-sm font-medium select-none hover:text-fg focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 text-fg-subtle transition-transform group-open:rotate-90" aria-hidden />
        {title}
        {meta ? <span className="ml-auto text-xs font-normal text-fg-muted num">{meta}</span> : null}
      </summary>
      <div className="space-y-3 pb-4 pl-6">{children}</div>
    </details>
  );
}

function sameAddress(a: string | null | undefined, b: string): boolean {
  return !!a && a.trim().toLowerCase() === b.trim().toLowerCase();
}

/**
 * Avancerat för en plats: alias, slå ihop och ta bort. Hopfällt som standard –
 * det vanliga (namn, typ, position) ligger i formuläret ovanför.
 */
export function PlaceAdvanced({
  place,
  places,
  open,
  onOpenChange,
  mergeTarget,
  onMergeTarget,
  onMerged,
  onDeleted,
}: {
  place: Place;
  places: Place[];
  open: AdvancedSection | null;
  onOpenChange: (section: AdvancedSection | null) => void;
  mergeTarget: string;
  onMergeTarget: (id: string) => void;
  /** Anropas med id för platsen som behölls. */
  onMerged: (keepId: string) => void;
  onDeleted: () => void;
}) {
  const others = React.useMemo(
    () => places.filter((p) => p.id !== place.id && p.is_demo === place.is_demo).sort((a, b) => a.name.localeCompare(b.name, "sv")),
    [places, place.id, place.is_demo],
  );
  const toggle = (section: AdvancedSection) => (o: boolean) => onOpenChange(o ? section : open === section ? null : open);
  const openMerge = (targetId?: string) => {
    if (targetId) onMergeTarget(targetId);
    onOpenChange("merge");
  };

  return (
    <div className="rounded-lg border border-border px-3">
      <Fold id="place-alias" title="Alias" meta={plural(place.aliases.length, "adress", "adresser")} open={open === "alias"} onOpenChange={toggle("alias")}>
        <AliasSection place={place} others={others} onMergeInstead={openMerge} />
      </Fold>
      <Fold id="place-merge" title="Slå ihop med annan plats" open={open === "merge"} onOpenChange={toggle("merge")}>
        <MergeSection place={place} others={others} target={mergeTarget} onTarget={onMergeTarget} onMerged={onMerged} />
      </Fold>
      <Fold id="place-delete" title="Ta bort plats" open={open === "delete"} onOpenChange={toggle("delete")}>
        <DeleteSection place={place} onDeleted={onDeleted} onMergeInstead={() => openMerge()} />
      </Fold>
    </div>
  );
}

// ----------------------------------------------------------------------------- alias

function AliasSection({ place, others, onMergeInstead }: { place: Place; others: Place[]; onMergeInstead: (targetId?: string) => void }) {
  const add = useAddAlias();
  const [address, setAddress] = React.useState("");
  const [error, setError] = React.useState<{ text: string; ownerId?: string; taken: boolean } | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const value = address.trim();
    if (!value) return;
    setError(null);
    void add.mutateAsync({ id: place.id, address: value }).then(
      () => {
        toast.success("Adressen är tillagd som alias.");
        setAddress("");
      },
      (err) => {
        const taken = err instanceof ApiError && err.code === "address_exists";
        const owner = taken ? others.find((p) => sameAddress(p.address, value) || p.aliases.some((a) => sameAddress(a, value))) : undefined;
        setError({ text: errorMessage(err), ownerId: owner?.id, taken });
      },
    );
  };

  return (
    <>
      <FieldHint>Adresser som ska räknas som den här platsen, t.ex. grannadressen där du brukar parkera.</FieldHint>
      {place.aliases.length ? (
        <ul className="space-y-1 text-sm" aria-label="Alias för platsen">
          {place.aliases.map((a) => (
            <li key={a} className="rounded-md bg-surface-2 px-2.5 py-1.5 break-words">
              {a}
            </li>
          ))}
        </ul>
      ) : null}
      <form onSubmit={submit} className="flex gap-2">
        <Input
          value={address}
          onChange={(e) => {
            setAddress(e.target.value);
            setError(null);
          }}
          placeholder="Gata 1, Ort"
          aria-label="Lägg till adress som alias"
          maxLength={300}
          autoComplete="off"
          aria-invalid={error ? true : undefined}
        />
        <Button type="submit" loading={add.isPending} disabled={!address.trim()}>
          {add.isPending ? null : <Plus aria-hidden />} Lägg till
        </Button>
      </form>
      {error ? (
        <div className="space-y-1.5">
          <FieldError>{error.text}</FieldError>
          {error.taken ? (
            <Button size="sm" onClick={() => onMergeInstead(error.ownerId)}>
              <Merge aria-hidden /> Slå ihop platserna
            </Button>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

// ----------------------------------------------------------------------------- slå ihop

function MergeSection({
  place,
  others,
  target,
  onTarget,
  onMerged,
}: {
  place: Place;
  others: Place[];
  target: string;
  onTarget: (id: string) => void;
  onMerged: (keepId: string) => void;
}) {
  const merge = useMergePlaces();
  const [keep, setKeep] = React.useState<"this" | "other">("this");
  const [error, setError] = React.useState<string | null>(null);
  const other = others.find((p) => p.id === target);

  if (!others.length) return <FieldHint>Det finns ingen annan plats att slå ihop med.</FieldHint>;

  const run = () => {
    if (!other) return;
    const keepPlace = keep === "this" ? place : other;
    const removePlace = keep === "this" ? other : place;
    setError(null);
    // mutateAsync + then: panelen kan hinna byta plats innan svaret hanteras.
    void merge.mutateAsync({ keepId: keepPlace.id, removeIds: [removePlace.id] }).then(
      () => {
        toast.success(`Platserna är ihopslagna till ${keepPlace.name}.`);
        onTarget("");
        onMerged(keepPlace.id);
      },
      (err) => setError(errorMessage(err)),
    );
  };

  return (
    <>
      <div className="space-y-1.5">
        <Label htmlFor="merge-target">Samma ställe som</Label>
        <Select value={target} onValueChange={onTarget}>
          <SelectTrigger id="merge-target">
            <SelectValue placeholder="Välj plats" />
          </SelectTrigger>
          <SelectContent>
            {others.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {other ? (
        <fieldset className="space-y-1.5">
          <legend className="mb-1.5 text-[13px] font-medium">Behåll</legend>
          {(
            [
              ["this", place],
              ["other", other],
            ] as const
          ).map(([value, p]) => (
            <label
              key={value}
              className={cn(
                "flex cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 text-sm transition-colors",
                keep === value ? "border-accent bg-accent-soft/50" : "border-border hover:border-border-strong",
              )}
            >
              <input type="radio" name="merge-keep" className="size-4 shrink-0 accent-accent" checked={keep === value} onChange={() => setKeep(value)} />
              <span className="min-w-0 flex-1 truncate">{p.name}</span>
              <span className="shrink-0 text-xs text-fg-muted num">{plural(p.visit_count, "besök", "besök")}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      <FieldHint>Resor, alias och inlärda mönster flyttas till platsen du behåller. Den andra platsen försvinner – det går inte att ångra.</FieldHint>
      <FieldError>{error}</FieldError>
      <Button onClick={run} disabled={!other} loading={merge.isPending}>
        {merge.isPending ? null : <Merge aria-hidden />} Slå ihop
      </Button>
    </>
  );
}

// ----------------------------------------------------------------------------- ta bort

function DeleteSection({ place, onDeleted, onMergeInstead }: { place: Place; onDeleted: () => void; onMergeInstead: () => void }) {
  const del = useDeletePlace();
  const [confirming, setConfirming] = React.useState(false);
  const [error, setError] = React.useState<{ text: string; inUse: boolean } | null>(null);

  const run = () => {
    setError(null);
    // mutateAsync + then: platsen (och panelen) försvinner när listan hämtats om.
    void del.mutateAsync(place.id).then(
      () => {
        toast.success(`${place.name} är borttagen.`);
        onDeleted();
      },
      (err) => {
        setConfirming(false);
        setError({ text: errorMessage(err), inUse: err instanceof ApiError && err.code === "in_use" });
      },
    );
  };

  return (
    <>
      <FieldHint>Bara platser som inte används av några resor kan tas bort. Används platsen kan du slå ihop den med en annan i stället.</FieldHint>
      {error ? (
        <div role="alert" className="space-y-2 rounded-md bg-err-soft px-3 py-2.5 text-sm text-err-ink">
          <p>{error.text}</p>
          {error.inUse ? (
            <Button size="sm" onClick={onMergeInstead}>
              <Merge aria-hidden /> Slå ihop med annan plats
            </Button>
          ) : null}
        </div>
      ) : null}
      {confirming ? (
        <div className="flex flex-wrap items-center gap-2">
          <p className="w-full text-sm">Ta bort platsen permanent? Det går inte att ångra.</p>
          <Button variant="danger" size="sm" onClick={run} loading={del.isPending}>
            Ja, ta bort
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setConfirming(false)} disabled={del.isPending}>
            Avbryt
          </Button>
        </div>
      ) : (
        <Button variant="danger-ghost" size="sm" className="-ml-2" onClick={() => setConfirming(true)}>
          <Trash2 aria-hidden /> Ta bort plats
        </Button>
      )}
    </>
  );
}
