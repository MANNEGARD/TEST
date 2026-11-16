"use client";

import { useMutation, useQueries, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { components } from "../api-types";
import type { Pattern, PatternStatus, Place, PlaceInput, PlaceSuggestions, RuleInput } from "../places-types";
import type { Feedback, Trip, TripDetail } from "../types";
import { useVehicle } from "../vehicle";
import { invalidateJournalData, qk } from "./keys";

/*
 * Platser och AI-minne.
 *   backend/app/api/routes/locations.py   GET/POST /locations, /locations/suggestions,
 *                                         PATCH/DELETE /locations/{id}, …/accept-suggestion,
 *                                         …/reject-suggestion, …/merge, …/aliases
 *   backend/app/api/routes/learning.py    GET /learning/patterns, POST /learning/rules,
 *                                         PATCH /learning/patterns/{id} (status),
 *                                         DELETE /learning/patterns/{id}/rule,
 *                                         GET /learning/feedback, POST …/{id}/undo
 *
 * Platslistan hämtas i sin helhet (personlig skala, högst 2 000) och söks, filtreras
 * och sorteras i klienten – då blir filtren omedelbara och samma lista kan användas
 * i väljare (slå ihop, regler) och på kartan.
 */

export const placeKeys = {
  all: qk.places.all,
  list: (includeDemo: boolean) => ["places", "list", { includeDemo }] as const,
  suggestions: ["places", "suggestions"] as const,
  meta: ["places", "meta"] as const,
  patterns: ["learning", "patterns"] as const,
  /** Ligger under qk.learning.feedback så att invalidateJournalData() även träffar den. */
  corrections: (limit: number) => [...qk.learning.feedback, "corrections", limit] as const,
};

/**
 * Invaliderar platser och mönster. Platsers namn och typ syns på resorna och styr
 * klassningen av oavgjorda resor, så journaldata invalideras också om inte
 * `journal: false` anges (t.ex. för ett nytt alias).
 */
export function invalidatePlaces(qc: QueryClient, opts: { journal?: boolean } = {}): Promise<unknown> {
  return Promise.all([
    qc.invalidateQueries({ queryKey: placeKeys.all }),
    qc.invalidateQueries({ queryKey: placeKeys.patterns }),
    opts.journal === false ? null : invalidateJournalData(qc),
  ]);
}

// ----------------------------------------------------------------------------- platser

/** Alla platser med besöksstatistik. Demoplatser tas bara med när ett demofordon är valt. */
export function usePlaces() {
  const { vehicle } = useVehicle();
  const includeDemo = !!vehicle?.is_demo;
  return useQuery({
    queryKey: placeKeys.list(includeDemo),
    queryFn: ({ signal }) =>
      api.get<Place[]>("/locations", { query: { sort: "visits", limit: 2000, include_demo: includeDemo || undefined }, signal }),
  });
}

export type LocationMeta = components["schemas"]["LocationMetaOut"];
export type HomeFromVehicleResult = components["schemas"]["HomeFromVehicleOut"];
export type HomeFromVehicleInput = components["schemas"]["HomeFromVehicleIn"];

/** Sammanfattningen av platserna, bl.a. om hemmet har koordinat (då slås adresser upp). */
export function useLocationMeta() {
  return useQuery({
    queryKey: placeKeys.meta,
    queryFn: ({ signal }) => api.get<LocationMeta>("/locations/meta", { signal }),
  });
}

/** "Bilen står hemma nu": bostaden markeras med bilens senast rapporterade position. */
export function useHomeFromVehicle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<HomeFromVehicleInput>) => api.post<HomeFromVehicleResult>("/locations/home-from-vehicle", body),
    onSuccess: () => invalidatePlaces(qc),
  });
}

export type AddressLookupResult = components["schemas"]["AddressLookupOut"];

/**
 * "Hämta adressen från kartan": slår upp just den platsens adress en gång (platsens position
 * skickas till karttjänsten). Förslaget sparas inte – det fylls i adressfältet.
 */
export function useLookupAddress() {
  return useMutation({
    mutationFn: (id: string) => api.post<AddressLookupResult>(`/locations/${id}/lookup-address`),
  });
}

export function usePlaceSuggestions() {
  return useQuery({
    queryKey: placeKeys.suggestions,
    queryFn: ({ signal }) => api.get<PlaceSuggestions>("/locations/suggestions", { signal }),
  });
}

export function useCreatePlace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PlaceInput) => api.post<Place>("/locations", body),
    onSuccess: () => invalidatePlaces(qc, { journal: false }),
  });
}

export function useUpdatePlace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: PlaceInput }) => api.patch<Place>(`/locations/${id}`, patch),
    onSuccess: () => invalidatePlaces(qc),
  });
}

export function useAnswerSuggestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, accept }: { id: string; accept: boolean }) =>
      api.post<Place>(`/locations/${id}/${accept ? "accept-suggestion" : "reject-suggestion"}`),
    onSuccess: () => invalidatePlaces(qc),
  });
}

/** Slår ihop `removeIds` in i `keepId` (en i taget – API:t tar ett par per anrop). */
export function useMergePlaces() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ keepId, removeIds }: { keepId: string; removeIds: string[] }) => {
      let kept: Place | undefined;
      for (const id of removeIds) kept = await api.post<Place>(`/locations/${id}/merge`, { into_id: keepId });
      return kept as Place;
    },
    // Även vid fel kan en del av sammanslagningarna ha gått igenom.
    onSettled: () => invalidatePlaces(qc),
  });
}

export function useAddAlias() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, address }: { id: string; address: string }) => api.post<Place>(`/locations/${id}/aliases`, { address }),
    onSuccess: () => invalidatePlaces(qc, { journal: false }),
  });
}

export function useDeletePlace() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.del(`/locations/${id}`),
    // Resor som pekade på platsen visar adressen i stället – körjournalen läses om.
    onSuccess: () => invalidatePlaces(qc),
  });
}

// ----------------------------------------------------------------------------- AI-minne

export function usePatterns() {
  return useQuery({
    queryKey: placeKeys.patterns,
    queryFn: ({ signal }) => api.get<Pattern[]>("/learning/patterns", { query: { limit: 500 }, signal }),
  });
}

export function useCreateRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RuleInput) => api.post<Pattern>("/learning/rules", body),
    onSuccess: () => invalidatePlaces(qc),
  });
}

/** Godkänn/avvisa ett inlärt mönster. Avvisade mönster används inte vid klassningen. */
export function useSetPatternStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: PatternStatus }) => api.patch<Pattern>(`/learning/patterns/${id}`, { status }),
    onSuccess: () => invalidatePlaces(qc),
  });
}

export function useDeleteRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patternId: string) => api.del(`/learning/patterns/${patternId}/rule`),
    onSuccess: () => invalidatePlaces(qc),
  });
}

/** De senaste korrigeringarna (ändrad kategori eller ändrat syfte), nyast först. */
export function useCorrections(limit = 8) {
  return useQuery({
    queryKey: placeKeys.corrections(limit),
    queryFn: ({ signal }) => api.get<Feedback[]>("/learning/feedback", { query: { only_corrections: true, limit }, signal }),
  });
}

export function useUndoCorrection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (feedbackId: string) => api.post<Trip>(`/learning/feedback/${feedbackId}/undo`),
    onSuccess: () => invalidatePlaces(qc),
  });
}

/**
 * Resorna bakom korrigeringarna (FeedbackOut innehåller bara resans id). Delar cache
 * med resedetaljvyn. Saknade resor (borttagna/sammanslagna) ger `undefined`.
 */
export function useTripsById(ids: string[]): Map<string, Trip> {
  const unique = [...new Set(ids)];
  const results = useQueries({
    queries: unique.map((id) => ({
      queryKey: qk.trips.detail(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => api.get<TripDetail>(`/trips/${id}`, { signal }),
      staleTime: 60_000,
      retry: false,
    })),
  });
  const map = new Map<string, Trip>();
  results.forEach((r, i) => {
    if (r.data) map.set(unique[i], r.data.trip);
  });
  return map;
}
