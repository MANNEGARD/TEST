"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { Gap, GapList, ReviewItem, ReviewList, Trip } from "../types";
import { useVehicle } from "../vehicle";
import { invalidateJournalData, qk } from "./keys";

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
  return data?.open_total;
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
