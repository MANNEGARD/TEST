"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { Gap, GapList, ReviewItem, ReviewList, Trip } from "../types";
import { useVehicle } from "../vehicle";
import { invalidateJournalData, qk } from "./keys";

/**
 * Ärendetyper som inte visas i granskningen. Användningsfallet är milersättning:
 * luckor i mätarställningen är privat körning – inte fel – och visas därför inte.
 */
export const HIDDEN_REVIEW_KINDS: readonly string[] = ["data_gap"];

export function visibleOpenCount(list: ReviewList | undefined): number | undefined {
  if (!list) return undefined;
  return Object.entries(list.open_counts).reduce((sum, [k, n]) => (HIDDEN_REVIEW_KINDS.includes(k) ? sum : sum + n), 0);
}

export function useReviewItems(params: { kind?: string[]; status?: string; limit?: number } = {}) {
  const { vehicleId } = useVehicle();
  const query = { status: "open", limit: 200, ...params, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.review.list(vehicleId, query),
    queryFn: ({ signal }) => api.get<ReviewList>("/review", { query, signal }),
    enabled: !!vehicleId,
  });
}

/** Antal öppna ärenden (för sidomenyns märke). Delar cache med listan. */
export function useOpenReviewCount() {
  const { data } = useReviewItems();
  return visibleOpenCount(data);
}

export function useGaps(onlyOpen = false) {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: [...qk.gaps.list(vehicleId), onlyOpen],
    queryFn: ({ signal }) => api.get<GapList>("/gaps", { query: { vehicle_id: vehicleId, only_open: onlyOpen }, signal }),
    enabled: !!vehicleId,
  });
}

export function useAnswerMerge() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, accept }: { itemId: string; accept: boolean }) =>
      api.post<{ merged: boolean; trip: Trip | null }>(`/merge-questions/${itemId}/answer`, { accept }),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export function useReviewAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, action, minutes }: { itemId: string; action: "dismiss" | "resolve" | "snooze" | "reopen"; minutes?: number }) =>
      api.post<ReviewItem>(`/review/${itemId}/${action}`, action === "snooze" ? { minutes: minutes ?? 1440 } : {}),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export function useGapAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ gapId, action, note }: { gapId: string; action: "resolve-private" | "ignore" | "reopen"; note?: string }) =>
      api.post<Gap | Trip>(`/gaps/${gapId}/${action}`, action === "reopen" ? undefined : { note: note ?? null }),
    onSuccess: () => invalidateJournalData(qc),
  });
}

/** Undo för en sammanslagning som gjorts via fråga: ångra operationen i loggen. */
export function useUndoMergeOperation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (operationId: string) => api.post<Trip[]>(`/merges/${operationId}/undo`),
    onSuccess: () => invalidateJournalData(qc),
  });
}

/**
 * Ångra en uppdelning ("Dela resan här") direkt från kvittot. Split-svaret innehåller inte
 * operationens id, så det slås upp i loggen (GET /merges) via de två nya resornas id:n.
 */
export function useUndoSplit() {
  const qc = useQueryClient();
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: async (resultTripIds: string[]) => {
      const ops = await api.get<{ id: string; operation: string; result_trip_ids: string[] | null; undone_at: string | null }[]>(
        "/merges",
        { query: { vehicle_id: vehicleId, limit: 20 } },
      );
      const op = ops.find((o) => o.operation === "split" && !o.undone_at && resultTripIds.every((id) => o.result_trip_ids?.includes(id)));
      if (!op) throw new Error("Uppdelningen hittades inte i loggen.");
      return api.post<Trip[]>(`/merges/${op.id}/undo`);
    },
    onSuccess: () => invalidateJournalData(qc),
  });
}
