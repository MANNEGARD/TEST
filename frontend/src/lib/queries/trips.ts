"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, type Query } from "../api";
import type { BulkUpdate, CategoryCode, ManualTrip, Trip, TripDetail, TripList, TripUpdate } from "../types";
import { useVehicle } from "../vehicle";
import { invalidateJournalData, qk } from "./keys";

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
    mutationFn: ({ id, patch }: { id: string; patch: TripUpdate }) => api.patch<Trip>(`/trips/${id}`, patch),
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
    mutationFn: (id: string) => api.post<Trip>(`/trips/${id}/approve`),
    onMutate: async (id) => {
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

export function useDeleteManualTrip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/trips/${id}`),
    onSuccess: () => invalidateJournalData(qc),
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

export function useReclassify() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<{ changed: boolean; note: string | null }>(`/trips/${id}/classify`),
    onSuccess: () => invalidateJournalData(qc),
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

/** Ångra en korrigering (lärande-feedback), t.ex. senaste kategoriändringen. */
export function useUndoFeedback() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (feedbackId: string) => api.post<Trip>(`/learning/feedback/${feedbackId}/undo`),
    onSuccess: () => invalidateJournalData(qc),
  });
}
