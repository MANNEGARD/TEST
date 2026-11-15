"use client";

import * as React from "react";
import { Gauge, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError, Input, Label } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatOdometer, formatTime, localDateISO, plural, zonedToISO } from "@/lib/format";
import { useAddOdometerReading, useDeleteOdometerReading, useOdometerReadings } from "@/lib/queries/settings-page";
import type { OdometerKind, OdometerSnapshot } from "@/lib/settings-page-types";
import type { Vehicle } from "@/lib/types";
import { UnitInput } from "./form-parts";
import { parseNumber } from "./use-settings-form";

/** Källan för en mätarställning (backend: VehicleOdometerSnapshot.source). */
const SOURCE_LABELS: Record<string, string> = {
  api: "Från bilen",
  import: "Importerad",
  manual: "Egen avläsning",
  year_start: "Årets början",
  year_end: "Årets slut",
};

const KIND_OPTIONS: { value: OdometerKind; label: string }[] = [
  { value: "manual", label: "Egen avläsning" },
  { value: "year_start", label: "Årets början" },
  { value: "year_end", label: "Årets slut" },
];

const MANUAL_KINDS = new Set<string>(KIND_OPTIONS.map((k) => k.value));
const MAX_KM = 5_000_000; // routes/vehicles.py → SnapshotIn
const PREVIEW = 8;

function AddReading({ vehicleId }: { vehicleId: string }) {
  const add = useAddOdometerReading();
  const [f, setF] = React.useState(() => ({ date: localDateISO(), time: formatTime(new Date()), km: "", kind: "manual" as OdometerKind, note: "" }));
  const [error, setError] = React.useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const km = parseNumber(f.km);
    if (!f.date || !f.time) return setError("Ange datum och klockslag för avläsningen.");
    if (km === null) return setError("Ange mätarställningen i km.");
    if (Number.isNaN(km) || km < 0 || km > MAX_KM) return setError("Mätarställningen ska vara ett tal mellan 0 och 5 000 000 km.");
    setError(null);
    add.mutate(
      { vehicleId, body: { at: zonedToISO(f.date, f.time), odometer_km: km, kind: f.kind, note: f.note.trim() } },
      {
        onSuccess: () => {
          toast.success("Mätarställningen är sparad.");
          setF((prev) => ({ ...prev, km: "", note: "" }));
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} noValidate className="rounded-lg border border-border bg-surface-2/40 p-4" aria-label="Lägg till mätarställning">
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_7rem_minmax(0,1fr)]">
        <div className="space-y-1.5">
          <Label htmlFor="odo-date" className="block">
            Datum
          </Label>
          <Input id="odo-date" type="date" value={f.date} max={localDateISO()} onChange={(e) => setF({ ...f, date: e.target.value })} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="odo-time" className="block">
            Klockslag
          </Label>
          <Input id="odo-time" type="time" value={f.time} onChange={(e) => setF({ ...f, time: e.target.value })} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="odo-km" className="block">
            Mätarställning
          </Label>
          <UnitInput
            id="odo-km"
            unit="km"
            inputMode="numeric"
            value={f.km}
            onChange={(e) => setF({ ...f, km: e.target.value })}
            placeholder="t.ex. 182 991"
            wrapperClassName="sm:w-full"
          />
        </div>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-[11rem_minmax(0,1fr)_auto] sm:items-end">
        <div className="space-y-1.5">
          <Label htmlFor="odo-kind" className="block">
            Typ
          </Label>
          <Select value={f.kind} onValueChange={(v) => setF({ ...f, kind: v as OdometerKind })}>
            <SelectTrigger id="odo-kind">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {KIND_OPTIONS.map((k) => (
                <SelectItem key={k.value} value={k.value}>
                  {k.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="odo-note" className="block">
            Anteckning <span className="font-normal text-fg-subtle">(valfritt)</span>
          </Label>
          <Input id="odo-note" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} maxLength={500} autoComplete="off" />
        </div>
        <Button type="submit" variant="secondary" loading={add.isPending}>
          {add.isPending ? null : <Plus aria-hidden />} Lägg till
        </Button>
      </div>
      <FieldError className="mt-2.5">{error}</FieldError>
    </form>
  );
}

function ReadingRow({ reading, vehicleId }: { reading: OdometerSnapshot; vehicleId: string }) {
  const remove = useDeleteOdometerReading();
  const add = useAddOdometerReading();
  const removable = MANUAL_KINDS.has(reading.source);

  const onDelete = () =>
    remove.mutate(
      { vehicleId, snapshotId: reading.id },
      {
        onSuccess: () =>
          toast.success("Mätarställningen är borttagen.", {
            action: {
              label: "Ångra",
              onClick: () =>
                add.mutate(
                  { vehicleId, body: { at: reading.at, odometer_km: reading.odometer_km, kind: reading.source as OdometerKind, note: reading.note } },
                  { onError: (err) => toast.error(errorMessage(err)) },
                ),
            },
          }),
        onError: (err) => toast.error(errorMessage(err)),
      },
    );

  return (
    <li className="flex items-center gap-3 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <time dateTime={reading.at} className="num text-[13px]">
            {formatDateTime(reading.at)}
          </time>
          <Badge size="sm" tone={removable ? "accent" : "neutral"}>
            {SOURCE_LABELS[reading.source] ?? reading.source}
          </Badge>
        </p>
        {reading.note ? <p className="mt-0.5 truncate text-xs text-fg-muted">{reading.note}</p> : null}
      </div>
      <span className="shrink-0 num text-sm font-medium">{formatOdometer(reading.odometer_km)}</span>
      {removable ? (
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onDelete}
          loading={remove.isPending}
          aria-label={`Ta bort mätarställningen ${formatDateTime(reading.at)}`}
        >
          {remove.isPending ? null : <Trash2 aria-hidden />}
        </Button>
      ) : (
        <span className="size-8 shrink-0" aria-hidden />
      )}
    </li>
  );
}

/** 12. Mätarställningar – lista och manuell avläsning (hopfälld under Fordon). */
export function OdometerReadings({ vehicle }: { vehicle: Vehicle }) {
  const readings = useOdometerReadings(vehicle.id);
  const [showAll, setShowAll] = React.useState(false);
  const rows = readings.data ?? [];
  const visible = showAll ? rows : rows.slice(0, PREVIEW);
  const latest = rows[0];

  return (
    <CollapsibleSection
      title="Mätarställningar"
      icon={<Gauge />}
      meta={
        readings.data ? (
          latest ? (
            <span className="num">{formatOdometer(latest.odometer_km)}</span>
          ) : (
            "Inga avläsningar"
          )
        ) : null
      }
    >
      <div className="space-y-4">
        <p className="max-w-2xl text-[13px] leading-relaxed text-fg-muted">
          Behövs normalt inte när du bara redovisar tjänsteresor – varje resa har redan sin egen mätarställning. Lägg till en avläsning om
          du vill ha en fast punkt, till exempel vid årsskiftet.
        </p>
        <AddReading vehicleId={vehicle.id} />
        {readings.isError ? (
          <ErrorState error={readings.error} onRetry={() => readings.refetch()} compact />
        ) : !readings.data ? (
          <SkeletonBlock rows={3} />
        ) : rows.length === 0 ? (
          <EmptyState
            compact
            icon={<Gauge />}
            title="Inga mätarställningar ännu"
            description="Här samlas avläsningar som bilen rapporterar via Volvo-kopplingen och de du lägger till själv."
          />
        ) : (
          <div>
            <ul className="divide-y divide-border" aria-label="Kända mätarställningar">
              {visible.map((r) => (
                <ReadingRow key={r.id} reading={r} vehicleId={vehicle.id} />
              ))}
            </ul>
            {rows.length > PREVIEW ? (
              <Button variant="ghost" size="sm" className="mt-2" onClick={() => setShowAll((s) => !s)} aria-expanded={showAll}>
                {showAll ? "Visa färre" : `Visa alla ${plural(rows.length, "avläsning", "avläsningar")}`}
              </Button>
            ) : null}
          </div>
        )}
      </div>
    </CollapsibleSection>
  );
}
