"use client";

import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { errorMessage } from "@/lib/api";
import { CATEGORY_LABELS } from "@/lib/labels";
import { invalidateJournalData } from "@/lib/queries/keys";
import { undoLatestFeedback } from "@/lib/queries/learning";
import { useApproveTrip, useBulkUpdate, useUpdateTrip } from "@/lib/queries/trips";
import type { CategoryCode } from "@/lib/types";

/**
 * Vanliga åtgärder på resor med toast + "Ångra". Används av körjournal,
 * resedetalj och granskning så att beteendet blir likadant överallt.
 */
export function useTripActions() {
  const qc = useQueryClient();
  const update = useUpdateTrip();
  const approve = useApproveTrip();
  const bulk = useBulkUpdate();

  const undoAction = (ids: string[], since: number, onUndone?: () => void) => ({
    label: "Ångra",
    onClick: () => {
      undoLatestFeedback(ids, since)
        .then((n) => {
          if (n) onUndone?.();
          void invalidateJournalData(qc);
          toast.success(n ? "Ändringen är ångrad." : "Det fanns ingen ändring att ångra.");
        })
        .catch((err) => toast.error(errorMessage(err)));
    },
  });

  return {
    isPending: update.isPending || approve.isPending || bulk.isPending,

    /** `onUndone` körs när ägaren ångrar ändringen från kvittot (t.ex. för att visa ärendet igen). */
    setCategory(id: string, category: CategoryCode, opts: { silent?: boolean; onUndone?: () => void } = {}) {
      const since = Date.now();
      return update.mutateAsync({ id, patch: { category } }).then(
        (t) => {
          if (!opts.silent) toast.success(`Kategori: ${CATEGORY_LABELS[category]}`, { action: undoAction([id], since, opts.onUndone) });
          return t;
        },
        (err) => {
          toast.error(errorMessage(err));
          throw err;
        },
      );
    },

    setPurpose(id: string, purpose: string) {
      const since = Date.now();
      return update.mutateAsync({ id, patch: { purpose } }).then(
        (t) => {
          toast.success("Syftet är sparat.", { action: undoAction([id], since) });
          return t;
        },
        (err) => {
          toast.error(errorMessage(err));
          throw err;
        },
      );
    },

    toggleFavorite(id: string, isFavorite: boolean) {
      return update.mutateAsync({ id, patch: { is_favorite: isFavorite } }).catch((err) => {
        toast.error(errorMessage(err));
      });
    },

    /** `expectedCategory`: förslaget som visades. Har det hunnit ändras godkänns ingenting (409). */
    approve(id: string, opts: { expectedCategory?: CategoryCode; onUndone?: () => void } = {}) {
      const since = Date.now();
      return approve.mutateAsync({ id, expectedCategory: opts.expectedCategory }).then(
        (t) => {
          toast.success("Godkänd.", { action: undoAction([id], since, opts.onUndone) });
          return t;
        },
        (err) => {
          toast.error(errorMessage(err));
          throw err;
        },
      );
    },

    bulk(ids: string[], patch: { category?: CategoryCode; purpose?: string; approve?: boolean; is_favorite?: boolean }, message: string) {
      const since = Date.now();
      const learns = patch.category !== undefined || patch.purpose !== undefined || patch.approve;
      return bulk.mutateAsync({ trip_ids: ids, approve: false, ...patch }).then(
        (res) => {
          const failed = Array.isArray(res.errors) ? res.errors.length : 0;
          if (failed) toast.warning(`${message} ${failed} resor kunde inte ändras.`);
          else toast.success(message, learns ? { action: undoAction(ids, since) } : undefined);
          return res;
        },
        (err) => {
          toast.error(errorMessage(err));
          throw err;
        },
      );
    },
  };
}
