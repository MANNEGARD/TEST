"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import type { MonthClose, Question, RespondInput, RespondResult, ReviewVerdict, ReviewVerdictResult } from "../notifications-types";
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
    queryFn: ({ signal }) => api.get<TripDetail>(`/trips/${encodeURIComponent(tripId ?? "")}`, { signal }),
    enabled: !!tripId,
    retry: false,
  });
}

/** Månadsskiftets läge med månadens automatiskt godkända och antagna rader (GET /month-close). */
export function useMonthReview() {
  return useQuery({
    queryKey: ["notifications", "month-review"] as const,
    queryFn: ({ signal }) => api.get<MonthClose>("/month-close", { signal }),
  });
}

/** "Stämmer" eller "Fel" på en rad i månadens genomgång. */
export function useReviewVerdict() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ tripId, verdict }: { tripId: string; verdict: ReviewVerdict }) =>
      api.post<ReviewVerdictResult>(`/notifications/review-rows/${encodeURIComponent(tripId)}`, { verdict }),
    onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: notificationKeys.all }), invalidateJournalData(qc)]),
  });
}
