/**
 * Query-nycklar för TanStack Query. ALLA nycklar skapas här så att invalidering
 * blir konsekvent. Struktur: [område, typ, ...parametrar].
 *
 * Invalidera brett efter mutationer med `invalidateJournalData()` (resor,
 * granskning, dashboard och luckor hänger ihop – en ändrad kategori påverkar alla).
 */

import type { QueryClient } from "@tanstack/react-query";

export const qk = {
  auth: {
    me: ["auth", "me"] as const,
    setupStatus: ["auth", "setup-status"] as const,
  },
  vehicles: ["vehicles"] as const,
  settings: ["settings"] as const,
  trips: {
    all: ["trips"] as const,
    lists: ["trips", "list"] as const,
    list: (params: Record<string, unknown>) => ["trips", "list", params] as const,
    details: ["trips", "detail"] as const,
    detail: (id: string) => ["trips", "detail", id] as const,
  },
  review: {
    all: ["review"] as const,
    list: (vehicleId: string | undefined, params: Record<string, unknown>) => ["review", "list", vehicleId, params] as const,
  },
  gaps: {
    all: ["gaps"] as const,
    list: (vehicleId: string | undefined) => ["gaps", vehicleId] as const,
  },
  dashboard: {
    all: ["dashboard"] as const,
    part: (part: string, vehicleId: string | undefined, params: Record<string, unknown>) =>
      ["dashboard", part, vehicleId, params] as const,
  },
  learning: {
    feedback: ["learning", "feedback"] as const,
  },
} as const;

/** Invaliderar allt som beror på resornas innehåll. */
export function invalidateJournalData(qc: QueryClient): Promise<unknown> {
  return Promise.all([
    qc.invalidateQueries({ queryKey: qk.trips.all }),
    qc.invalidateQueries({ queryKey: qk.review.all }),
    qc.invalidateQueries({ queryKey: qk.dashboard.all }),
    qc.invalidateQueries({ queryKey: qk.gaps.all }),
    qc.invalidateQueries({ queryKey: qk.learning.feedback }),
  ]);
}
