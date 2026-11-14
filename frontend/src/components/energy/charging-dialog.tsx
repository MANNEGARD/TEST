"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import type { ChargingInput, ChargingRecord } from "@/lib/energy-types";
import { formatDate, formatDuration, formatTime, localDateISO, zonedToISO } from "@/lib/format";
import { addDays } from "@/lib/period";
import { useCreateCharging, useUpdateCharging } from "@/lib/queries/energy";
import { formError, nowTime, numberField, parseNumber } from "./form";

/** "Lägg till laddning" / "Ändra laddning". Sluttid före starttid tolkas som nästa dag (nattladdning). */
export function ChargingDialog({
  open,
  onOpenChange,
  record,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  record?: ChargingRecord | null;
  /** Anropas efter sparning – anroparen ger kvittot (toast). */
  onSaved: (record: ChargingRecord, what: "created" | "updated") => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{record ? "Ändra laddning" : "Lägg till laddning"}</DialogTitle>
          <DialogDescription>Laddningar är frivilliga – de används för att följa elkostnaden. Tider anges i din tidszon.</DialogDescription>
        </DialogHeader>
        <ChargingForm
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

function ChargingForm({ record, onDone }: { record: ChargingRecord | null; onDone: (saved?: ChargingRecord, what?: "created" | "updated") => void }) {
  const create = useCreateCharging();
  const update = useUpdateCharging();
  const pending = create.isPending || update.isPending;
  const [f, setF] = React.useState(() => ({
    date: record ? formatDate(record.started_at) : localDateISO(),
    start: record ? formatTime(record.started_at) : nowTime(),
    end: record?.ended_at ? formatTime(record.ended_at) : "",
    kwh: numberField(record?.kwh),
    cost: numberField(record?.total_cost),
    location: record?.location ?? "",
  }));
  const [error, setError] = React.useState<string | null>(null);
  const [touched, setTouched] = React.useState(false);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF((prev) => ({ ...prev, [k]: e.target.value }));

  const kwh = parseNumber(f.kwh);
  const cost = parseNumber(f.cost);
  const errors = {
    date: !f.date || !f.start ? "Ange datum och starttid." : null,
    kwh: kwh === null ? "Ange hur många kWh som laddades." : Number.isNaN(kwh) || kwh <= 0 || kwh > 1000 ? "Ange kWh som ett tal mellan 0 och 1 000." : null,
    cost: cost !== null && (Number.isNaN(cost) || cost > 1_000_000) ? "Ange beloppet i kronor, t.ex. 48,50." : null,
  };
  const firstError = errors.date ?? errors.kwh ?? errors.cost;

  // Slutar laddningen "före" den börjar har den pågått över midnatt.
  const overnight = !!f.end && !!f.start && f.end < f.start;
  const startISO = f.date && f.start ? zonedToISO(f.date, f.start) : null;
  const endISO = f.date && f.end ? zonedToISO(overnight ? addDays(f.date, 1) : f.date, f.end) : null;
  const seconds = startISO && endISO ? (new Date(endISO).getTime() - new Date(startISO).getTime()) / 1000 : null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setTouched(true);
    setError(null);
    if (firstError || kwh === null || !startISO) return;
    const body: ChargingInput = {
      started_at: startISO,
      ended_at: endISO,
      kwh,
      total_cost: cost,
      location: f.location.trim() || null,
    };
    const onError = (err: unknown) => setError(formError(err));
    if (!record) {
      create.mutate(body, { onSuccess: (saved) => onDone(saved, "created"), onError });
      return;
    }
    update.mutate({ id: record.id, patch: body }, { onSuccess: (saved) => onDone(saved, "updated"), onError });
  };

  return (
    <form onSubmit={submit} className="grid gap-4" noValidate>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <div className="col-span-2 space-y-1.5 sm:col-span-1">
          <Label htmlFor="charge-date">Datum</Label>
          <Input id="charge-date" type="date" required value={f.date} onChange={set("date")} aria-invalid={touched && !!errors.date} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="charge-start">Start</Label>
          <Input id="charge-start" type="time" required value={f.start} onChange={set("start")} aria-invalid={touched && !!errors.date} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="charge-end">
            Slut <span className="font-normal text-fg-muted">– valfritt</span>
          </Label>
          <Input id="charge-end" type="time" value={f.end} onChange={set("end")} aria-describedby="charge-end-hint" />
        </div>
      </div>
      {seconds != null ? (
        <FieldHint id="charge-end-hint" className="-mt-2">
          Laddningen pågick <span className="font-mono">{formatDuration(seconds)}</span>
          {overnight ? " och slutade nästa dag." : "."}
        </FieldHint>
      ) : null}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="charge-kwh">Laddat (kWh)</Label>
          <Input id="charge-kwh" inputMode="decimal" required autoFocus={!record} value={f.kwh} onChange={set("kwh")} placeholder="t.ex. 14,2" aria-invalid={touched && !!errors.kwh} autoComplete="off" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="charge-cost">
            Belopp (kr) <span className="font-normal text-fg-muted">– valfritt</span>
          </Label>
          <Input id="charge-cost" inputMode="decimal" value={f.cost} onChange={set("cost")} placeholder="t.ex. 48,50" aria-invalid={touched && !!errors.cost} autoComplete="off" />
        </div>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="charge-location">
          Plats <span className="font-normal text-fg-muted">– valfritt</span>
        </Label>
        <Input id="charge-location" value={f.location} onChange={set("location")} maxLength={200} placeholder="t.ex. Hemma eller Ionity Jönköping" />
      </div>
      <FieldError>{error ?? (touched ? firstError : null)}</FieldError>
      <DialogFooter>
        <Button variant="ghost" onClick={() => onDone()}>
          Avbryt
        </Button>
        <Button type="submit" variant="primary" loading={pending}>
          {record ? "Spara ändringar" : "Spara laddning"}
        </Button>
      </DialogFooter>
    </form>
  );
}
