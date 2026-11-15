"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import type { Question, RespondInput, RespondResult } from "../notifications-types";
import type { TripDetail } from "../types";
import { invalidateJournalData, qk } from "./keys";

/*
 * Notisfrågor (backend/app/api/routes/notifications.py):
 *   GET  /notifications/{public_id}          → fråga med resa/underlag
 *   POST /notifications/{public_id}/respond  {action, reply_text?, idempotency_key} → RespondResult
 *
 * Svaret kan ändra resan (kategori, syfte, sammanslagning) – därför invalideras
 * körjournalens data tillsammans med frågan.
 */

export const notificationKeys = {
  all: ["notifications"] as const,
  detail: (publicId: string) => ["notifications", "detail", publicId] as const,
};

export function useQuestion(publicId: string) {
  return useQuery({
    queryKey: notificationKeys.detail(publicId),
    queryFn: ({ signal }) => api.get<Question>(`/notifications/${encodeURIComponent(publicId)}`, { signal }),
    // En fråga som inte finns blir inte till av att man frågar igen.
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });
}

export function useRespond(publicId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RespondInput) => api.post<RespondResult>(`/notifications/${encodeURIComponent(publicId)}/respond`, body),
    onSuccess: (res) =>
      Promise.all([
        qc.invalidateQueries({ queryKey: notificationKeys.all }),
        res.outcome === "applied" ? invalidateJournalData(qc) : undefined,
      ]),
  });
}

/**
 * Resans detaljer (platsnamn, tidernas kvalitet, syftesförslag) för frågans resa.
 * Samma nyckel som resedetaljvyn, så cachen delas. Saknas resan visas frågans egen
 * sammanfattning – därför inga omförsök.
 */
export function useQuestionTrip(tripId: string | null | undefined) {
  return useQuery({
    queryKey: qk.trips.detail(tripId ?? ""),
    queryFn: ({ signal }) => api.get<TripDetail>(`/trips/${tripId}`, { signal }),
    enabled: !!tripId,
    retry: false,
  });
}
