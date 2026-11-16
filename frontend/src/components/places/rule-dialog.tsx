"use client";

import * as React from "react";
import { Briefcase, CircleHelp, Home } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, errorMessage } from "@/lib/api";
import type { Place, PurposeMode } from "@/lib/places-types";
import { cn } from "@/lib/utils";
import { useCreateRule } from "@/lib/queries/places";

export interface RulePreset {
  originId?: string | null;
  destinationId?: string;
  category?: "work" | "private" | "undecided" | null;
  purpose?: string | null;
  purposeMode?: PurposeMode | null;
}

const PURPOSE_MODES: { value: PurposeMode; label: string }[] = [
  { value: "unless_calendar", label: "Använd texten när kalendern inte säger något annat (rekommenderas)" },
  { value: "always", label: "Använd alltid texten" },
];

type CategoryChoice = "work" | "private" | "undecided" | "none";
const ANY = "any";

/**
 * Skapar en manuell regel (POST /learning/rules): resor till en plats – eller en
 * rutt, om "endast från" väljs – klassas alltid som Arbete/Privat, föreslås aldrig
 * automatiskt ("Fråga mig alltid" = category "undecided") och/eller får ett standardsyfte. En befintlig regel för samma plats/rutt ersätts.
 */
export function RuleDialog({
  open,
  onOpenChange,
  places,
  preset,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  places: Place[];
  preset?: RulePreset;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>Ny regel</DialogTitle>
          <DialogDescription>Bestäm hur resor till en plats alltid ska klassas. Redan avgjorda resor ändras inte.</DialogDescription>
        </DialogHeader>
        {open ? <RuleForm places={places} preset={preset} onDone={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function RuleForm({ places, preset, onDone }: { places: Place[]; preset?: RulePreset; onDone: () => void }) {
  const create = useCreateRule();
  const sorted = React.useMemo(() => [...places].sort((a, b) => a.name.localeCompare(b.name, "sv")), [places]);
  const [destination, setDestination] = React.useState(preset?.destinationId ?? "");
  const [origin, setOrigin] = React.useState(preset?.originId ?? ANY);
  const [category, setCategory] = React.useState<CategoryChoice>(preset?.category ?? "work");
  const [purpose, setPurpose] = React.useState(preset?.purpose ?? "");
  const [purposeMode, setPurposeMode] = React.useState<PurposeMode>(preset?.purposeMode ?? "unless_calendar");
  const [error, setError] = React.useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!destination) return setError("Välj vilken plats regeln gäller.");
    if (category === "none" && !purpose.trim()) return setError("Välj en kategori eller ange ett standardsyfte.");
    setError(null);
    void create
      .mutateAsync({
        destination_location_id: destination,
        origin_location_id: origin === ANY ? null : origin,
        category: category === "none" ? null : category,
        purpose: purpose.trim() || null,
        purpose_mode: purpose.trim() ? purposeMode : null,
      })
      .then(
        () => {
          toast.success("Regeln är sparad.");
          onDone();
        },
        (err) =>
          setError(
            // Äldre backend känner inte till "Fråga mig alltid" (category "undecided") och svarar 422.
            category === "undecided" && err instanceof ApiError && err.status === 422
              ? "Servern kan inte spara ”Fråga mig alltid” ännu – uppdatera DriveLog och försök igen."
              : errorMessage(err),
          ),
      );
  };

  return (
    <form onSubmit={submit} className="grid gap-4" noValidate>
      <div className="space-y-1.5">
        <Label htmlFor="rule-destination">Resor till</Label>
        <Select
          value={destination}
          onValueChange={(v) => {
            setDestination(v);
            setError(null);
          }}
        >
          <SelectTrigger id="rule-destination">
            <SelectValue placeholder="Välj plats" />
          </SelectTrigger>
          <SelectContent>
            {sorted.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="rule-origin">
          Endast från <span className="font-normal text-fg-muted">(valfritt)</span>
        </Label>
        <Select value={origin} onValueChange={setOrigin}>
          <SelectTrigger id="rule-origin">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Var som helst</SelectItem>
            {sorted.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <p id="rule-category-label" className="text-[13px] font-medium">
          Kategori för resorna
        </p>
        <SegmentedControl<CategoryChoice>
          ariaLabel="Kategori för resorna"
          size="lg"
          className="grid w-full grid-cols-2"
          value={category}
          onChange={(v) => {
            setCategory(v);
            setError(null);
          }}
          options={[
            { value: "work", label: "Alltid arbete", icon: <Briefcase aria-hidden />, activeClassName: "data-[state=on]:bg-work-soft data-[state=on]:text-work-ink" },
            { value: "private", label: "Alltid privat", icon: <Home aria-hidden />, activeClassName: "data-[state=on]:bg-private-soft data-[state=on]:text-private-ink" },
            {
              value: "undecided",
              label: "Fråga mig alltid",
              icon: <CircleHelp aria-hidden />,
              activeClassName: "data-[state=on]:bg-undecided-soft data-[state=on]:text-undecided-ink",
            },
            { value: "none", label: "Ingen kategori", title: "Regeln sätter bara ett standardsyfte" },
          ]}
        />
        {category === "undecided" ? (
          <FieldHint>Resorna klassas aldrig automatiskt – du får alltid frågan om arbete eller privat.</FieldHint>
        ) : null}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="rule-purpose">
          Standardsyfte <span className="font-normal text-fg-muted">(valfritt)</span>
        </Label>
        <Input
          id="rule-purpose"
          value={purpose}
          onChange={(e) => {
            setPurpose(e.target.value);
            setError(null);
          }}
          maxLength={500}
          placeholder="t.ex. Kundbesök hos Exempelbolaget"
          autoComplete="off"
        />
        <FieldHint>Fylls i som syfte på resorna. Du kan alltid ändra en enskild resa.</FieldHint>
      </div>
      {purpose.trim() ? (
        <fieldset className="space-y-1.5" data-testid="rule-purpose-mode">
          <legend className="text-[13px] font-medium">När texten gäller</legend>
          {PURPOSE_MODES.map((m) => (
            <label
              key={m.value}
              className={cn(
                "flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2 text-[13px] leading-snug transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring",
                purposeMode === m.value ? "border-accent bg-accent-soft/50 text-fg" : "border-border text-fg-muted hover:bg-surface-2/50",
              )}
            >
              <input
                type="radio"
                name="rule-purpose-mode"
                value={m.value}
                checked={purposeMode === m.value}
                onChange={() => setPurposeMode(m.value)}
                className="mt-0.5 accent-accent"
              />
              {m.label}
            </label>
          ))}
          <FieldHint>
            {purposeMode === "always"
              ? "Texten används även när en kalenderbokning stämmer med resan."
              : "Stämmer en kalenderbokning med resan föreslås ärendet ur bokningen i stället."}
          </FieldHint>
        </fieldset>
      ) : null}
      <FieldError>{error}</FieldError>
      <DialogFooter>
        <Button variant="ghost" onClick={onDone}>
          Avbryt
        </Button>
        <Button type="submit" variant="primary" loading={create.isPending}>
          Spara regel
        </Button>
      </DialogFooter>
    </form>
  );
}
