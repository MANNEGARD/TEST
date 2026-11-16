"use client";

import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { DeleteConsequences, useJournalScope } from "@/components/trip-detail/delete-trip-dialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { errorMessage } from "@/lib/api";
import { formatKm, formatNumber, plural } from "@/lib/format";
import { useBulkDeleteTrips } from "@/lib/queries/trips";
import type { Trip } from "@/lib/types";

function ReturnText({ imported, manual }: { imported: number; manual: number }) {
  if (imported && manual) {
    return (
      <>
        <strong className="font-semibold">Importerade resor kommer tillbaka om du importerar samma fil igen</strong> – som de står i filen, utan dina ändringar.
        Manuellt inlagda resor kommer inte tillbaka.
      </>
    );
  }
  if (imported) {
    return imported === 1 ? (
      <>
        <strong className="font-semibold">Importerar du samma fil igen kommer resan tillbaka</strong> – som den står i filen, utan de ändringar du har gjort.
      </>
    ) : (
      <>
        <strong className="font-semibold">Importerar du samma fil igen kommer resorna tillbaka</strong> – som de står i filen, utan de ändringar du har gjort.
      </>
    );
  }
  return <>{manual === 1 ? "En manuellt inlagd resa kommer inte tillbaka. Lägg in den igen om du ångrar dig." : "Manuellt inlagda resor kommer inte tillbaka. Lägg in dem igen om du ångrar dig."}</>;
}

/**
 * "Ta bort" i körjournalens massfält (POST /trips/bulk-delete). Bara importerade och manuellt
 * inlagda resor kan tas bort; resor från bilen räknas upp men skickas inte. Servern prövar
 * varje resa för sig (sammanslagen, ingår i en tjänsteresa …) och svarar med ett fel per resa.
 */
export function BulkDeleteDialog({
  trips,
  open,
  onOpenChange,
  onDone,
}: {
  /** De valda resor som webbappen känner till. */
  trips: Trip[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Efter ett svar från servern (markeringen töms). */
  onDone: () => void;
}) {
  const scope = useJournalScope();
  const remove = useBulkDeleteTrips();
  const deletable = trips.filter((t) => t.deletable);
  const blocked = trips.length - deletable.length;
  const n = deletable.length;
  const km = deletable.reduce((sum, t) => sum + t.distance_km, 0);
  const imported = deletable.filter((t) => t.deletable === "imported").length;
  const manual = n - imported;
  const blockedText = blocked
    ? `${formatNumber(blocked, 0)} av de valda är ${blocked === 1 ? "en resa" : "resor"} från bilen och kan inte tas bort.`
    : null;

  const confirm = () =>
    remove.mutate(
      deletable.map((t) => t.id),
      {
        onSuccess: (res) => {
          onOpenChange(false);
          onDone();
          const failed = res.errors.length;
          if (res.deleted > 0) toast.success(`${plural(res.deleted, "resa", "resor")} togs bort.`);
          if (failed > 0) {
            const text = `${formatNumber(failed, 0)} kunde inte tas bort: ${res.errors[0].message}`;
            if (res.deleted > 0) toast.warning(text);
            else toast.error(text);
          }
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );

  return (
    <Dialog open={open} onOpenChange={(o) => !remove.isPending && onOpenChange(o)}>
      <DialogContent data-testid="bulk-delete-dialog">
        {n === 0 ? (
          <>
            <DialogHeader>
              <DialogTitle>Resorna kan inte tas bort</DialogTitle>
              <DialogDescription>{blockedText ?? "Inga av de valda resorna kan tas bort."} Bara importerade och manuellt inlagda resor kan tas bort.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="primary" onClick={() => onOpenChange(false)}>
                Stäng
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Ta bort {plural(n, "resa", "resor")}?</DialogTitle>
              <DialogDescription>
                {plural(n, "resa", "resor")} (<span className="num">{formatKm(km)}</span>) tas bort ur körjournalen och underlaget.{" "}
                {n === 1 ? "Den raderas inte för gott" : "De raderas inte för gott"}: borttagningen syns i historiken
                {imported ? `, och originalraden${imported === 1 ? "" : "erna"} finns kvar under Import` : ""}.
              </DialogDescription>
            </DialogHeader>
            <DeleteConsequences scope={scope} />
            <p className="text-sm leading-relaxed text-fg">
              <ReturnText imported={imported} manual={manual} />
            </p>
            {blockedText ? <p className="rounded-md bg-surface-2 px-3 py-2 text-[13px] text-fg-muted">{blockedText}</p> : null}
            <DialogFooter>
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={remove.isPending}>
                Avbryt
              </Button>
              <Button variant="danger" onClick={confirm} loading={remove.isPending} data-testid="bulk-delete-confirm">
                {remove.isPending ? null : <Trash2 aria-hidden />} Ta bort {plural(n, "resa", "resor")}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
