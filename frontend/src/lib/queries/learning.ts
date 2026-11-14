"use client";

import { api } from "../api";
import type { Feedback, Trip } from "../types";

/**
 * Ångrar den senaste (ej ångrade) korrigeringen för var och en av resorna via
 * POST /learning/feedback/{id}/undo. Används av "Ångra" i toasts.
 * `since` (ms) begränsar till korrigeringar gjorda efter åtgärden startade.
 */
export async function undoLatestFeedback(tripIds: string[], since?: number): Promise<number> {
  const list = await api.get<Feedback[]>("/learning/feedback", { query: { limit: Math.min(500, Math.max(20, tripIds.length * 3)) } });
  const wanted = new Set(tripIds);
  const seen = new Set<string>();
  const targets: Feedback[] = [];
  for (const fb of list) {
    if (!wanted.has(fb.logical_trip_id) || seen.has(fb.logical_trip_id) || fb.undone_at) continue;
    if (since && new Date(fb.created_at).getTime() < since - 5000) continue;
    seen.add(fb.logical_trip_id);
    targets.push(fb);
  }
  for (const fb of targets) await api.post<Trip>(`/learning/feedback/${fb.id}/undo`);
  return targets.length;
}
