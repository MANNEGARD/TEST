"use client";

import * as React from "react";
import { Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, errorMessage } from "@/lib/api";
import { formatKm, formatNumber } from "@/lib/format";
import { useAllocate, type AllocationPart } from "@/lib/queries/trips";
import type { CategoryCode, RawTrip, TripSegment } from "@/lib/types";

interface Row {
  km: string;
  category: CategoryCode;
  note: string;
}

function parseKm(v: string): number {
  return Number(v.replace(/\s/g, "").replace(",", "."));
}

/**
 * Fördela en delresa på privat/tjänst (POST /trips/{id}/allocate). Summan av
 * delsträckorna måste vara exakt resans sträcka – kontrolleras här och av backend
 * (felkod allocation_sum_mismatch).
 */
export function AllocationDialog({
  tripId,
  raw,
  segments,
  open,
  onOpenChange,
}: {
  tripId: string;
  raw: RawTrip;
  segments: TripSegment[];
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const allocate = useAllocate();
  const initial: Row[] = segments.length
    ? segments.map((s) => ({ km: formatNumber(s.distance_km, 1), category: s.category, note: s.note ?? "" }))
    : [{ km: formatNumber(raw.distance_km, 1), category: "work", note: "" }];
  const [rows, setRows] = React.useState<Row[]>(initial);
  const [error, setError] = React.useState<string | null>(null);

  const sum = rows.reduce((a, r) => a + (Number.isFinite(parseKm(r.km)) ? parseKm(r.km) : 0), 0);
  const diff = Math.round((raw.distance_km - sum) * 10) / 10;
  const matches = Math.abs(diff) < 0.05;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const parts: AllocationPart[] = rows.map((r) => ({ distance_km: parseKm(r.km), category: r.category, note: r.note.trim() || null }));
    if (parts.some((p) => !Number.isFinite(p.distance_km) || p.distance_km <= 0)) {
      setError("Varje delsträcka måste vara större än 0 km.");
      return;
    }
    // Justera sista delen för avrundning så att summan blir exakt.
    if (matches) parts[parts.length - 1].distance_km = Math.round((parts[parts.length - 1].distance_km + (raw.distance_km - sum)) * 1000) / 1000;
    allocate.mutate(
      { id: tripId, rawTripId: raw.id, parts },
      {
        onSuccess: () => {
          toast.success("Fördelningen är sparad.");
          onOpenChange(false);
        },
        onError: (err) => {
          if (err instanceof ApiError && err.code === "allocation_sum_mismatch") {
            const expected = typeof err.data.expected_km === "number" ? err.data.expected_km : raw.distance_km;
            const got = typeof err.data.got_km === "number" ? err.data.got_km : sum;
            setError(`Delsträckorna blir ${formatKm(got)} men resan är ${formatKm(expected)}. Justera så att summan stämmer.`);
          } else setError(errorMessage(err));
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Fördela privat och tjänst</DialogTitle>
          <DialogDescription>
            Dela upp delresan ({formatKm(raw.distance_km)}) i delsträckor, t.ex. när en tjänsteresa avslutades med ett privat ärende.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          {rows.map((r, i) => (
            <div key={i} className="grid grid-cols-[96px_130px_1fr_auto] items-center gap-2">
              <Input
                aria-label={`Sträcka del ${i + 1} (km)`}
                inputMode="decimal"
                value={r.km}
                onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, km: e.target.value } : x)))}
              />
              <Select value={r.category} onValueChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, category: v as CategoryCode } : x)))}>
                <SelectTrigger aria-label={`Kategori del ${i + 1}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="work">Arbete</SelectItem>
                  <SelectItem value="private">Privat</SelectItem>
                  <SelectItem value="undecided">Ej avgjord</SelectItem>
                </SelectContent>
              </Select>
              <Input
                aria-label={`Anteckning del ${i + 1}`}
                placeholder="Anteckning"
                value={r.note}
                onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, note: e.target.value } : x)))}
              />
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={`Ta bort del ${i + 1}`}
                disabled={rows.length <= 1}
                onClick={() => setRows(rows.filter((_, j) => j !== i))}
              >
                <Trash2 />
              </Button>
            </div>
          ))}
          <div className="flex items-center justify-between">
            <Button size="sm" variant="ghost" onClick={() => setRows([...rows, { km: matches ? "" : formatNumber(Math.max(0, diff), 1), category: "private", note: "" }])} disabled={rows.length >= 10}>
              <Plus aria-hidden /> Lägg till delsträcka
            </Button>
            <p className={matches ? "text-xs text-ok-ink" : "text-xs text-warn-ink"} aria-live="polite">
              Summa <span className="font-mono">{formatKm(sum)}</span> av <span className="font-mono">{formatKm(raw.distance_km)}</span>
              {matches ? " – stämmer" : ` – ${diff > 0 ? `${formatKm(diff)} kvar` : `${formatKm(-diff)} för mycket`}`}
            </p>
          </div>
          <FieldError>{error}</FieldError>
          <DialogFooter>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Avbryt
            </Button>
            <Button type="submit" variant="primary" disabled={!matches} loading={allocate.isPending}>
              Spara fördelning
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
