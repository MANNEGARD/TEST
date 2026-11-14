"use client";

import * as React from "react";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import type { FuelInput, FuelRecord } from "@/lib/energy-types";
import { formatDate, formatNumber, formatTime, localDateISO, zonedToISO } from "@/lib/format";
import { useCreateFuel, useUpdateFuel } from "@/lib/queries/energy";
import { formError, nowTime, numberField, parseNumber, round2 } from "./form";

export type SavedKind = "created" | "updated" | "confirmed";

/**
 * "Lägg till tankning" / "Ändra tankning". Ett förslag ur tanknivån (source = detected)
 * räknas som bekräftat när användaren sparar – volymen märks då som uppmätt.
 */
export function FuelDialog({
  open,
  onOpenChange,
  record,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Tankningen som ändras; utelämnas för en ny. */
  record?: FuelRecord | null;
  /** Anropas efter sparning – anroparen ger kvittot (toast). */
  onSaved: (record: FuelRecord, what: SavedKind) => void;
}) {
  const suggestion = record?.source === "detected";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{!record ? "Lägg till tankning" : suggestion ? "Bekräfta tankning" : "Ändra tankning"}</DialogTitle>
          <DialogDescription>
            {suggestion
              ? "Förslaget kommer ur bilens tanknivå. Rätta volymen efter kvittot och spara för att bekräfta."
              : "Tankningar är frivilliga – de används för att följa bränslekostnaden. Tider anges i din tidszon."}
          </DialogDescription>
        </DialogHeader>
        <FuelForm
          record={record ?? null}
          onDone={(saved, what) => {
            onOpenChange(false);
            if (saved && what) onSaved(saved, what);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function FuelForm({ record, onDone }: { record: FuelRecord | null; onDone: (saved?: FuelRecord, what?: SavedKind) => void }) {
  const create = useCreateFuel();
  const update = useUpdateFuel();
  const pending = create.isPending || update.isPending;
  const suggestion = record?.source === "detected";
  const [f, setF] = React.useState(() => ({
    date: record ? formatDate(record.at) : localDateISO(),
    time: record ? formatTime(record.at) : nowTime(),
    liters: numberField(record?.liters),
    cost: numberField(record?.total_cost),
    odometer: numberField(record?.odometer_km, 0),
    station: record?.station ?? "",
    fullTank: record?.full_tank ?? false,
  }));
  const [error, setError] = React.useState<string | null>(null);
  const [touched, setTouched] = React.useState(false);
  const set = (k: "date" | "time" | "liters" | "cost" | "odometer" | "station") => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((prev) => ({ ...prev, [k]: e.target.value }));

  const liters = parseNumber(f.liters);
  const cost = parseNumber(f.cost);
  const odometer = parseNumber(f.odometer);
  const errors = {
    date: !f.date || !f.time ? "Ange datum och klockslag." : null,
    liters: liters === null ? "Ange hur många liter du tankade." : Number.isNaN(liters) || liters <= 0 || liters > 1000 ? "Ange liter som ett tal mellan 0 och 1 000." : null,
    cost: cost !== null && (Number.isNaN(cost) || cost > 1_000_000) ? "Ange beloppet i kronor, t.ex. 825,50." : null,
    odometer: odometer !== null && (Number.isNaN(odometer) || odometer > 5_000_000) ? "Ange mätarställningen i hela km." : null,
  };
  const firstError = errors.date ?? errors.liters ?? errors.cost ?? errors.odometer;
  const perLiter = liters && cost && !Number.isNaN(liters) && !Number.isNaN(cost) && liters > 0 ? cost / liters : null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setTouched(true);
    setError(null);
    if (firstError || liters === null) return;
    const body: FuelInput = {
      at: zonedToISO(f.date, f.time),
      liters,
      total_cost: cost,
      odometer_km: odometer,
      station: f.station.trim() || null,
      full_tank: f.fullTank,
    };
    const onError = (err: unknown) => setError(formError(err));
    if (!record) {
      create.mutate(body, { onSuccess: (saved) => onDone(saved, "created"), onError });
      return;
    }
    // PATCH räknar inte om literpriset – skicka det när beloppet är känt, töm det annars.
    const patch: Partial<FuelInput> = { ...body, liters_quality: "verified", price_per_liter: cost != null ? round2(cost / liters) : null };
    update.mutate({ id: record.id, patch }, { onSuccess: (saved) => onDone(saved, suggestion ? "confirmed" : "updated"), onError });
  };

  return (
    <form onSubmit={submit} className="grid gap-4" noValidate>
      {suggestion && record?.note ? (
        <p className="flex items-start gap-2 rounded-md border border-border bg-surface-2/60 px-3 py-2 text-xs leading-relaxed text-fg-muted">
          <Sparkles className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {record.note}
        </p>
      ) : null}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="fuel-date">Datum</Label>
          <Input id="fuel-date" type="date" required value={f.date} onChange={set("date")} aria-invalid={touched && !!errors.date} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="fuel-time">Klockslag</Label>
          <Input id="fuel-time" type="time" required value={f.time} onChange={set("time")} aria-invalid={touched && !!errors.date} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="fuel-liters">Liter</Label>
          <Input id="fuel-liters" inputMode="decimal" required autoFocus={!record} value={f.liters} onChange={set("liters")} placeholder="t.ex. 42,5" aria-invalid={touched && !!errors.liters} autoComplete="off" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="fuel-cost">
            Belopp (kr) <span className="font-normal text-fg-muted">– valfritt</span>
          </Label>
          <Input id="fuel-cost" inputMode="decimal" value={f.cost} onChange={set("cost")} placeholder="t.ex. 825,50" aria-invalid={touched && !!errors.cost} aria-describedby="fuel-cost-hint" autoComplete="off" />
        </div>
      </div>
      <FieldHint id="fuel-cost-hint" className="-mt-2">
        {perLiter != null ? (
          <>
            Motsvarar <span className="font-mono">{formatNumber(perLiter, 2, 2)} kr/l</span>.
          </>
        ) : (
          "Med belopp visas kostnad och literpris."
        )}
      </FieldHint>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="fuel-odometer">
            Mätarställning (km) <span className="font-normal text-fg-muted">– valfritt</span>
          </Label>
          <Input id="fuel-odometer" inputMode="numeric" value={f.odometer} onChange={set("odometer")} placeholder="t.ex. 182 991" aria-invalid={touched && !!errors.odometer} autoComplete="off" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="fuel-station">
            Station <span className="font-normal text-fg-muted">– valfritt</span>
          </Label>
          <Input id="fuel-station" value={f.station} onChange={set("station")} maxLength={200} placeholder="t.ex. Circle K Jönköping" />
        </div>
      </div>
      <label className="flex w-fit items-center gap-2.5 text-sm">
        <Checkbox checked={f.fullTank} onCheckedChange={(v) => setF((prev) => ({ ...prev, fullTank: v === true }))} />
        Full tank
      </label>
      <FieldError>{error ?? (touched ? firstError : null)}</FieldError>
      <DialogFooter>
        <Button variant="ghost" onClick={() => onDone()}>
          Avbryt
        </Button>
        <Button type="submit" variant="primary" loading={pending}>
          {suggestion ? "Bekräfta tankning" : record ? "Spara ändringar" : "Spara tankning"}
        </Button>
      </DialogFooter>
    </form>
  );
}
