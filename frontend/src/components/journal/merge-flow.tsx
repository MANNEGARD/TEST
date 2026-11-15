"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ApiError, errorMessage } from "@/lib/api";
import { formatKm } from "@/lib/format";
import { useMergeTrips } from "@/lib/queries/trips";
import type { Trip } from "@/lib/types";

/**
 * Sammanslagning av valda resor (POST /trips/merge). Om backend svarar
 * `gap_requires_ack` visas en bekräftelse som förklarar luckan; vid ja skickas
 * anropet igen med acknowledge_gap = true.
 */
export function useMergeFlow(onDone?: (trip: Trip) => void) {
  const merge = useMergeTrips();
  const [pending, setPending] = React.useState<{ ids: string[]; detail: string; gapKm: number | null } | null>(null);

  const run = React.useCallback(
    (ids: string[], acknowledge = false) => {
      merge.mutate(
        { trip_ids: ids, acknowledge_gap: acknowledge },
        {
          onSuccess: (trip) => {
            setPending(null);
            toast.success(`${ids.length} resor slogs samman.`, { description: "Du kan ångra sammanslagningen på resans sida." });
            onDone?.(trip);
          },
          onError: (err) => {
            if (err instanceof ApiError && err.code === "gap_requires_ack") {
              setPending({ ids, detail: err.detail, gapKm: typeof err.data.gap_km === "number" ? err.data.gap_km : null });
            } else {
              setPending(null);
              toast.error(errorMessage(err));
            }
          },
        },
      );
    },
    [merge, onDone],
  );

  const dialog = (
    <Dialog open={!!pending} onOpenChange={(o) => !o && setPending(null)}>
      <DialogContent>
        <DialogHeader>
          <div className="mb-1 flex size-10 items-center justify-center rounded-full bg-warn-soft text-warn-ink">
            <AlertTriangle className="size-5" aria-hidden />
          </div>
          <DialogTitle>Det finns en lucka mellan resorna</DialogTitle>
          <DialogDescription>{pending?.detail}</DialogDescription>
        </DialogHeader>
        <div className="rounded-md border border-border bg-surface-2 p-3 text-sm text-fg-muted">
          {pending?.gapKm != null ? (
            <p>
              <strong className="num text-fg">{formatKm(pending.gapKm)}</strong> i mätarställningen saknar journalpost mellan resorna. Luckan
              finns kvar och redovisas som ej journalförd även efter sammanslagningen.
            </p>
          ) : (
            <p>Luckan finns kvar och redovisas som ej journalförd även efter sammanslagningen.</p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setPending(null)}>
            Avbryt
          </Button>
          <Button variant="primary" loading={merge.isPending} onClick={() => pending && run(pending.ids, true)}>
            Slå samman ändå
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return { run, dialog, isPending: merge.isPending };
}
