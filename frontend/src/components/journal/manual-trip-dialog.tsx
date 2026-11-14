"use client";

import * as React from "react";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, errorMessage } from "@/lib/api";
import { localDateISO, zonedToISO } from "@/lib/format";
import { useCreateManualTrip } from "@/lib/queries/trips";
import type { CategoryCode } from "@/lib/types";
import { useVehicle } from "@/lib/vehicle";

function num(v: string): number | null {
  if (!v.trim()) return null;
  const n = Number(v.replace(/\s/g, "").replace(",", "."));
  return Number.isFinite(n) ? n : NaN;
}

/** "Lägg till resa manuellt" (POST /trips/manual). */
export function ManualTripDialog({ trigger }: { trigger?: React.ReactNode }) {
  const { vehicleId } = useVehicle();
  const create = useCreateManualTrip();
  const [open, setOpen] = React.useState(false);
  const [f, setF] = React.useState({
    date: localDateISO(),
    start: "08:00",
    end: "09:00",
    from: "",
    to: "",
    km: "",
    odoStart: "",
    odoEnd: "",
    category: "undecided" as CategoryCode,
    purpose: "",
  });
  const [error, setError] = React.useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });

  const km = num(f.km);
  const odoS = num(f.odoStart);
  const odoE = num(f.odoEnd);
  const problems: string[] = [];
  if (f.end < f.start) problems.push("Sluttiden ligger före starttiden.");
  if (km !== null && (Number.isNaN(km) || km <= 0)) problems.push("Ange avståndet i km (större än 0).");
  if ((odoS === null) !== (odoE === null)) problems.push("Ange både mätarställning start och slut, eller ingen av dem.");
  if (odoS !== null && odoE !== null && odoE < odoS) problems.push("Mätarställning slut är lägre än start.");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (problems.length || !f.from.trim() || !f.to.trim() || km === null) {
      setError(problems[0] ?? "Fyll i adresser och avstånd.");
      return;
    }
    create.mutate(
      {
        vehicle_id: vehicleId,
        started_at: zonedToISO(f.date, f.start),
        ended_at: zonedToISO(f.date, f.end),
        start_address: f.from.trim(),
        end_address: f.to.trim(),
        distance_km: km,
        odometer_start: odoS,
        odometer_end: odoE,
        category: f.category,
        purpose: f.purpose.trim() || null,
      },
      {
        onSuccess: () => {
          toast.success("Resan är tillagd.");
          setOpen(false);
          setF({ ...f, from: "", to: "", km: "", odoStart: "", odoEnd: "", purpose: "" });
        },
        onError: (err) => setError(err instanceof ApiError && err.fields.length ? `${err.detail} (${err.fields.map((x) => x.field).join(", ")})` : errorMessage(err)),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button variant="primary">
            <Plus aria-hidden /> Lägg till resa
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Lägg till resa manuellt</DialogTitle>
          <DialogDescription>Manuella resor märks som &bdquo;Manuellt inmatad&rdquo;. Tider anges i din tidszon.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-3 space-y-1.5 sm:col-span-1">
              <Label htmlFor="mt-date">Datum</Label>
              <Input id="mt-date" type="date" required value={f.date} onChange={set("date")} />
            </div>
            <div className="space-y-1.5 ">
              <Label htmlFor="mt-start">Start</Label>
              <Input id="mt-start" type="time" required value={f.start} onChange={set("start")} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="mt-end">Slut</Label>
              <Input id="mt-end" type="time" required value={f.end} onChange={set("end")} />
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="mt-from">Från (adress)</Label>
              <Input id="mt-from" required value={f.from} onChange={set("from")} autoComplete="off" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="mt-to">Till (adress)</Label>
              <Input id="mt-to" required value={f.to} onChange={set("to")} autoComplete="off" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="mt-km">Avstånd (km)</Label>
              <Input id="mt-km" inputMode="decimal" required value={f.km} onChange={set("km")} placeholder="t.ex. 42,5" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="mt-os">Mätare start</Label>
              <Input id="mt-os" inputMode="numeric" value={f.odoStart} onChange={set("odoStart")} placeholder="Valfritt" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="mt-oe">Mätare slut</Label>
              <Input id="mt-oe" inputMode="numeric" value={f.odoEnd} onChange={set("odoEnd")} placeholder="Valfritt" />
            </div>
          </div>
          <FieldHint>Utan mätarställning markeras resan som ofullständig.</FieldHint>
          <div className="grid gap-3 sm:grid-cols-[180px_1fr]">
            <div className="space-y-1.5">
              <Label htmlFor="mt-cat">Kategori</Label>
              <Select value={f.category} onValueChange={(v) => setF({ ...f, category: v as CategoryCode })}>
                <SelectTrigger id="mt-cat">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="undecided">Ej avgjord</SelectItem>
                  <SelectItem value="work">Arbete</SelectItem>
                  <SelectItem value="private">Privat</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="mt-purpose">Syfte</Label>
              <Input id="mt-purpose" value={f.purpose} onChange={set("purpose")} maxLength={500} placeholder="t.ex. Kundbesök hos …" />
            </div>
          </div>
          <FieldError>{error}</FieldError>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Avbryt
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              Spara resa
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
