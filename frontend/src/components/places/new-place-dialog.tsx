"use client";

import * as React from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { ApiError, errorMessage } from "@/lib/api";
import type { Place } from "@/lib/places-types";
import { useCreatePlace } from "@/lib/queries/places";
import { LocationTypeSelect } from "./location-type";

const EMPTY = { name: "", type: "other", address: "", company: "" };

/** "Ny plats" (POST /locations). Position sätts efteråt i platsens sidopanel. */
export function NewPlaceDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Anropas när användaren väljer att öppna den nya platsen. */
  onCreated: (place: Place) => void;
}) {
  const create = useCreatePlace();
  const [f, setF] = React.useState(EMPTY);
  const [error, setError] = React.useState<{ text: string; field?: "name" | "address" } | null>(null);
  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setF({ ...f, [k]: e.target.value });
    setError(null);
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!f.name.trim()) {
      setError({ text: "Ange ett namn på platsen.", field: "name" });
      return;
    }
    setError(null);
    void create.mutateAsync({ name: f.name.trim(), type: f.type, address: f.address.trim() || null, company: f.company.trim() || null }).then(
      (place) => {
        toast.success(`${place.name} är tillagd.`, { action: { label: "Sätt position", onClick: () => onCreated(place) } });
        onOpenChange(false);
        setF(EMPTY);
      },
      (err) =>
        setError({
          text: err instanceof ApiError && err.fields.length ? `${err.detail} (${err.fields.map((x: { field: string }) => x.field).join(", ")})` : errorMessage(err),
          field: err instanceof ApiError && err.code === "address_exists" ? "address" : undefined,
        }),
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Ny plats</DialogTitle>
          <DialogDescription>Lägg till en plats du kör till i tjänsten, t.ex. en kund. Resor till adressen kopplas till platsen.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <div className="space-y-1.5">
            <Label htmlFor="np-name">Namn</Label>
            <Input id="np-name" value={f.name} onChange={set("name")} maxLength={200} required autoComplete="off" placeholder="t.ex. Exempelbolaget" aria-invalid={error?.field === "name" || undefined} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="np-type">Typ</Label>
            <LocationTypeSelect id="np-type" value={f.type} onChange={(type) => setF({ ...f, type })} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="np-address">
              Adress <span className="font-normal text-fg-muted">(valfritt)</span>
            </Label>
            <Input id="np-address" value={f.address} onChange={set("address")} maxLength={300} autoComplete="off" placeholder="Gata 1, Ort" aria-invalid={error?.field === "address" || undefined} />
            <FieldHint>Skriv adressen som den står i körjournalen.</FieldHint>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="np-company">
              Företag <span className="font-normal text-fg-muted">(valfritt)</span>
            </Label>
            <Input id="np-company" value={f.company} onChange={set("company")} maxLength={200} autoComplete="off" />
          </div>
          <FieldError>{error?.text}</FieldError>
          <DialogFooter>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Avbryt
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              Lägg till plats
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
