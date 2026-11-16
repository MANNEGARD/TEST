"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, DECISION_TIMEOUT_MS, type Query } from "../api";
import type {
  AiReviewPlan,
  AiReviewPlanInput,
  BulkUpdate,
  CategoryCode,
  ManualTrip,
  ReclassifyResult,
  Trip,
  TripDetail,
  TripList,
  TripUpdate,
} from "../types";
import { useVehicle } from "../vehicle";
import { invalidateJournalData, qk } from "./keys";

/** En bedömning med AI kan ta en stund; hänger leverantören ger webbappen upp och går vidare. */
const AI_TIMEOUT_MS = 90_000;

/** Parametrar för GET /trips (se backend/app/api/routes/trips.py → list_trips). */
export interface TripListParams {
  from?: string;
  to?: string;
  category?: string[];
  review_status?: string[];
  source?: string[];
  q?: string;
  needs_review?: boolean;
  favorite?: boolean;
  journey_id?: string;
  location_id?: string;
  sort?: "date" | "distance" | "category" | "status" | "start" | "end" | "odometer" | "purpose";
  order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}

export function useTrips(params: TripListParams) {
  const { vehicleId } = useVehicle();
  const full = { ...params, vehicle_id: vehicleId };
  return useQuery({
    queryKey: qk.trips.list(full),
    queryFn: ({ signal }) => api.get<TripList>("/trips", { query: full as Query, signal }),
    enabled: !!vehicleId,
    placeholderData: keepPreviousData,
  });
}

export function useTripDetail(id: string) {
  return useQuery({
    queryKey: qk.trips.detail(id),
    queryFn: ({ signal }) => api.get<TripDetail>(`/trips/${id}`, { signal }),
  });
}

// ----------------------------------------------------------------------------- optimistisk uppdatering

/** Räknar om km-fördelningen när hela resan byter kategori. */
function withCategory(t: Trip, category: CategoryCode): Trip {
  return {
    ...t,
    category,
    work_km: category === "work" ? t.distance_km : 0,
    private_km: category === "private" ? t.distance_km : 0,
    undecided_km: category === "undecided" ? t.distance_km : 0,
    has_mixed_categories: false,
  };
}

function patchTripLocally(t: Trip, patch: TripUpdate): Trip {
  let next = { ...t };
  if (patch.category) next = withCategory(next, patch.category as CategoryCode);
  if (patch.purpose !== undefined && patch.purpose !== null) next.purpose = patch.purpose;
  if (patch.is_favorite !== undefined && patch.is_favorite !== null) next.is_favorite = patch.is_favorite;
  if (patch.category || patch.approve) next.review_status = "approved";
  return next;
}

type Snapshot = [readonly unknown[], unknown][];

/** Uppdaterar resan i alla cachade listor och i detaljvyn. Returnerar snapshot för rollback. */
export function optimisticTripPatch(qc: QueryClient, ids: string[], patch: TripUpdate): Snapshot {
  const set = new Set(ids);
  const snapshot: Snapshot = [];
  for (const [key, data] of qc.getQueriesData<TripList>({ queryKey: qk.trips.lists })) {
    snapshot.push([key, data]);
    if (!data) continue;
    qc.setQueryData<TripList>(key, {
      ...data,
      items: data.items.map((t) => (set.has(t.id) ? patchTripLocally(t, patch) : t)),
    });
  }
  for (const id of ids) {
    const key = qk.trips.detail(id);
    const data = qc.getQueryData<TripDetail>(key);
    snapshot.push([key, data]);
    if (data) qc.setQueryData<TripDetail>(key, { ...data, trip: patchTripLocally(data.trip, patch) });
  }
  return snapshot;
}

export function restoreSnapshot(qc: QueryClient, snapshot: Snapshot | undefined) {
  for (const [key, data] of snapshot ?? []) qc.setQueryData(key, data);
}

// ----------------------------------------------------------------------------- mutationer

export function useUpdateTrip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: TripUpdate }) =>
      api.patch<Trip>(`/trips/${id}`, patch, { timeoutMs: DECISION_TIMEOUT_MS }),
    onMutate: async ({ id, patch }) => {
      await qc.cancelQueries({ queryKey: qk.trips.all });
      return { snapshot: optimisticTripPatch(qc, [id], patch) };
    },
    onError: (_e, _v, ctx) => restoreSnapshot(qc, ctx?.snapshot),
    onSettled: () => invalidateJournalData(qc),
  });
}

export function useApproveTrip() {
  const qc = useQueryClient();
  return useMutation({
    /** `expected`: förslaget (kategori och syfte) som ägaren såg. Har det hunnit ändras svarar servern 409. */
    mutationFn: ({ id, expected }: { id: string; expected?: { category: CategoryCode; purpose: string | null } }) =>
      api.post<Trip>(
        `/trips/${id}/approve`,
        expected ? { expected_category: expected.category, expected_purpose: expected.purpose } : undefined,
        { timeoutMs: DECISION_TIMEOUT_MS },
      ),
    onMutate: async ({ id }) => {
      await qc.cancelQueries({ queryKey: qk.trips.all });
      return { snapshot: optimisticTripPatch(qc, [id], { approve: true }) };
    },
    onError: (_e, _v, ctx) => restoreSnapshot(qc, ctx?.snapshot),
    onSettled: () => invalidateJournalData(qc),
  });
}

export function useBulkUpdate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: BulkUpdate) => api.post<{ updated: number; errors: unknown[] }>("/trips/bulk", body),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: qk.trips.all });
      const { trip_ids, ...patch } = body;
      return { snapshot: optimisticTripPatch(qc, trip_ids, patch) };
    },
    onError: (_e, _v, ctx) => restoreSnapshot(qc, ctx?.snapshot),
    onSettled: () => invalidateJournalData(qc),
  });
}

export function useMergeTrips() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { trip_ids: string[]; acknowledge_gap?: boolean }) => api.post<Trip>("/trips/merge", body),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export function useUnmerge() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Trip[]>(`/trips/${id}/unmerge`),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export function useSplitTrip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, afterIndex }: { id: string; afterIndex: number }) =>
      api.post<Trip[]>(`/trips/${id}/split`, { after_index: afterIndex }),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export interface AllocationPart {
  distance_km: number;
  category: CategoryCode;
  note?: string | null;
}

export function useAllocate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, rawTripId, parts }: { id: string; rawTripId: string; parts: AllocationPart[] }) =>
      api.post<Trip>(`/trips/${id}/allocate`, { raw_trip_id: rawTripId, parts }),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export function useClearAllocation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, rawTripId }: { id: string; rawTripId: string }) => api.del<Trip>(`/trips/${id}/allocate/${rawTripId}`),
    onSuccess: () => invalidateJournalData(qc),
  });
}

export function useCreateManualTrip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ManualTrip) => api.post<Trip>("/trips/manual", body),
    onSuccess: () => invalidateJournalData(qc),
  });
}

/**
 * Efter borttagning: allt som beror på resorna hämtas om, utom detaljvyn för de borttagna
 * resorna – den markeras bara som inaktuell så att vyn inte hinner visa "Resan finns inte"
 * innan webbappen har gått vidare till körjournalen.
 */
function invalidateAfterDelete(qc: QueryClient, ids: string[]): void {
  const gone = new Set(ids);
  const isGone = (key: readonly unknown[]) => key[1] === "detail" && gone.has(key[2] as string);
  void qc.invalidateQueries({ queryKey: qk.trips.details, predicate: (q) => isGone(q.queryKey), refetchType: "none" });
  void qc.invalidateQueries({ queryKey: qk.trips.all, predicate: (q) => !isGone(q.queryKey) });
  for (const key of [qk.review.all, qk.dashboard.all, qk.gaps.all, qk.learning.feedback, qk.exports.all, qk.places.all, qk.energy.all]) {
    void qc.invalidateQueries({ queryKey: key });
  }
}

/** Ta bort en importerad eller manuellt inlagd resa (DELETE /trips/{id}). Resan makuleras, den raderas inte. */
export function useDeleteTrip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/trips/${id}`),
    onSuccess: (_d, id) => invalidateAfterDelete(qc, [id]),
  });
}

/** Svar från POST /trips/bulk-delete. */
export interface BulkDeleteResult {
  deleted: number;
  errors: { trip_id: string; code: string; message: string }[];
}

/** Ta bort flera valda resor. Varje resa prövas för sig; de som inte går ger ett fel var. */
export function useBulkDeleteTrips() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (tripIds: string[]) => api.post<BulkDeleteResult>("/trips/bulk-delete", { trip_ids: tripIds }),
    onSuccess: (_d, ids) => invalidateAfterDelete(qc, ids),
  });
}

export function useCalendarMatchChoice(tripId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ matchId, selected }: { matchId: string; selected: boolean }) =>
      api.post(`/trips/${tripId}/calendar-matches/${matchId}`, { selected }),
    onSuccess: () => invalidateJournalData(qc),
  });
}

/**
 * Bedöm en resa på nytt (POST /trips/{id}/classify): reglerna och, om en leverantör är vald, AI.
 * Godkända och importerade resor rörs aldrig av servern (`outcome: "skipped_decided"`).
 * `invalidate: false` används av AI-genomgången, som hämtar om allt först när den är klar.
 */
export function useReclassify({ invalidate = true }: { invalidate?: boolean } = {}) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<ReclassifyResult>(`/trips/${id}/classify`, undefined, { timeoutMs: AI_TIMEOUT_MS }),
    onSuccess: () => {
      if (invalidate) void invalidateJournalData(qc);
    },
  });
}

/**
 * Planen för en AI-genomgång (POST /trips/ai-review/plan): vilka resor som bedöms, hur många
 * som hoppas över och vilken leverantör som används. Ändrar ingenting.
 */
export function useAiReviewPlan() {
  const { vehicleId } = useVehicle();
  return useMutation({
    mutationFn: (body: AiReviewPlanInput) => api.post<AiReviewPlan>("/trips/ai-review/plan", body, { query: { vehicle_id: vehicleId } }),
  });
}

/**
 * "Ärendet stämmer inte" (POST /trips/{id}/purpose/reject-calendar): ett ärende som sattes ur
 * kalendern utan fråga tas bort, kalenderträffen väljs inte igen och resan får ett ärende att
 * bekräfta i Granskning.
 */
export function useRejectCalendarPurpose() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Trip>(`/trips/${id}/purpose/reject-calendar`, undefined, { timeoutMs: DECISION_TIMEOUT_MS }),
    onSettled: () => invalidateJournalData(qc),
  });
}

export function useUpdateJourney() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; name?: string; purpose?: string; confirm?: boolean }) =>
      api.patch(`/journeys/${id}`, body),
    onSuccess: () => invalidateJournalData(qc),
  });
}

/** En överordnad tjänsteresa (GET /journeys, backend/app/api/routes/trips.py → list_journeys). */
export interface JourneySummary {
  id: string;
  name: string;
  purpose: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  distance_km: number;
  trip_count: number;
}

/** Fordonets tjänsteresor. Nyckeln ligger under qk.trips.all så att invalidateJournalData() träffar den. */
export function useJourneys(enabled = true) {
  const { vehicleId } = useVehicle();
  return useQuery({
    queryKey: [...qk.trips.all, "journeys", vehicleId] as const,
    queryFn: ({ signal }) => api.get<JourneySummary[]>("/journeys", { query: { vehicle_id: vehicleId }, signal }),
    enabled: enabled && !!vehicleId,
  });
}

/** "Inte en tjänsteresa": löser upp grupperingen. Körningarna och deras klassning påverkas inte. */
export function useDeleteJourney() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/journeys/${id}`),
    onSuccess: () => invalidateJournalData(qc),
  });
}

/** Ångra en korrigering (lärande-feedback), t.ex. senaste kategoriändringen. */
export function useUndoFeedback() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (feedbackId: string) => api.post<Trip>(`/learning/feedback/${feedbackId}/undo`),
    onSuccess: () => invalidateJournalData(qc),
  });
}
