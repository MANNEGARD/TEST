"use client";

import * as React from "react";
import { Briefcase, Home } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/api";
import type { Place } from "@/lib/places-types";
import { useCreateRule } from "@/lib/queries/places";

export interface RulePreset {
  originId?: string | null;
  destinationId?: string;
  category?: "work" | "private" | null;
  purpose?: string | null;
}

type CategoryChoice = "work" | "private" | "none";
const ANY = "any";

/**
 * Skapar en manuell regel (POST /learning/rules): resor till en plats – eller en
 * rutt, om "endast från" väljs – klassas alltid som Arbete/Privat och/eller får ett
 * standardsyfte. En befintlig regel för samma plats/rutt ersätts.
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
      })
      .then(
        () => {
          toast.success("Regeln är sparad.");
          onDone();
        },
        (err) => setError(errorMessage(err)),
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
          Klassa alltid som
        </p>
        <SegmentedControl<CategoryChoice>
          ariaLabel="Klassa alltid som"
          size="lg"
          className="w-full [&>button]:flex-1"
          value={category}
          onChange={(v) => {
            setCategory(v);
            setError(null);
          }}
          options={[
            { value: "work", label: "Arbete", icon: <Briefcase aria-hidden />, activeClassName: "data-[state=on]:bg-work-soft data-[state=on]:text-work-ink" },
            { value: "private", label: "Privat", icon: <Home aria-hidden />, activeClassName: "data-[state=on]:bg-private-soft data-[state=on]:text-private-ink" },
            { value: "none", label: "Ingen", title: "Regeln sätter bara ett standardsyfte" },
          ]}
        />
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
