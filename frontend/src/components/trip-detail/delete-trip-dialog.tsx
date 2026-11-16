"use client";

import { useRouter } from "next/navigation";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { errorMessage } from "@/lib/api";
import { formatDayMonth, formatKm } from "@/lib/format";
import { useSettings } from "@/lib/queries/settings";
import { useDeleteTrip } from "@/lib/queries/trips";
import type { Trip } from "@/lib/types";

/** Journalens omfång (general.journal_scope) – avgör vad kilometrarna blir efter borttagningen. */
export function useJournalScope(): "business_only" | "complete" {
  const settings = useSettings();
  const scope = (settings.data?.settings.general as { journal_scope?: string } | undefined)?.journal_scope;
  return scope === "complete" ? "complete" : "business_only";
}

/** Vad borttagningen gör (backend/app/services/trips/removal.py → delete_trip). Samma lista för en och flera resor. */
export function DeleteConsequences({ scope }: { scope: "business_only" | "complete" }) {
  return (
    <ul className="list-disc space-y-1.5 pl-5 text-sm leading-relaxed text-fg-muted marker:text-fg-subtle">
      <li>
        {scope === "complete"
          ? "Kilometrarna blir en lucka i mätarställningen som du får frågor om."
          : "Kilometrarna räknas därefter som övrig körning och redovisas inte."}
      </li>
      <li>Öppna frågor om resan stängs.</li>
      <li>Det du har bestämt om resan används inte längre för förslag. Egna regler som du har skapat finns kvar.</li>
      <li>Har underlaget för månaden redan skapats uppdateras det, på samma sätt som när du ändrar en resa.</li>
    </ul>
  );
}

/** "Hemmet → Exempel Åkeri AB" – bekräftade platsers namn, annars adressen (som i rubriken på resan). */
export function routeLabel(t: Trip): string {
  const start = t.start_location?.confirmed ? t.start_location.name : (t.start_address ?? "Okänd start");
  const end = t.end_location?.confirmed ? t.end_location.name : (t.end_address ?? "Okänt mål");
  return `${start} → ${end}`;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Bekräftelse för "Ta bort resan" (DELETE /trips/{id}). Resan makuleras med spår i historiken.
 * En importerad resa kommer tillbaka om samma fil importeras igen, en manuellt inlagd gör det inte.
 */
export function DeleteTripDialog({ trip, open, onOpenChange }: { trip: Trip; open: boolean; onOpenChange: (open: boolean) => void }) {
  const router = useRouter();
  const scope = useJournalScope();
  const remove = useDeleteTrip();
  const imported = trip.deletable !== "manual";
  const when = trip.started_at ?? trip.sort_time;

  const confirm = () =>
    remove.mutate(trip.id, {
      onSuccess: () => {
        onOpenChange(false);
        toast.success("Resan är borttagen.");
        router.push("/journal");
      },
      onError: (e) => toast.error(errorMessage(e)),
    });

  return (
    <Dialog open={open} onOpenChange={(o) => !remove.isPending && onOpenChange(o)}>
      <DialogContent data-testid="delete-trip-dialog">
        <DialogHeader>
          <DialogTitle>Ta bort resan?</DialogTitle>
          <DialogDescription>
            {capitalize(formatDayMonth(when))} {routeLabel(trip)}, <span className="num">{formatKm(trip.distance_km)}</span>, tas bort ur körjournalen och
            underlaget.{" "}
            {imported
              ? "Den raderas inte för gott: originalraden finns kvar under Import, och borttagningen syns i historiken."
              : "Den raderas inte för gott: borttagningen syns i historiken."}
          </DialogDescription>
        </DialogHeader>
        <DeleteConsequences scope={scope} />
        <p className="text-sm leading-relaxed text-fg">
          {imported ? (
            <>
              <strong className="font-semibold">Importerar du samma fil igen kommer resan tillbaka</strong> – som den står i filen, utan de ändringar du har gjort.
            </>
          ) : (
            "En manuellt inlagd resa kommer inte tillbaka. Lägg in den igen om du ångrar dig."
          )}
        </p>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={remove.isPending}>
            Avbryt
          </Button>
          <Button variant="danger" onClick={confirm} loading={remove.isPending} data-testid="delete-trip-confirm">
            {remove.isPending ? null : <Trash2 aria-hidden />} Ta bort resan
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
