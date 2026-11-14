"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { Vehicle } from "../types";
import { qk } from "./keys";

/** Alla fordon. Använd hellre `useVehicle()` (src/lib/vehicle.tsx) för valt fordon. */
export function useVehicles(enabled = true) {
  return useQuery({
    queryKey: qk.vehicles,
    queryFn: () => api.get<Vehicle[]>("/vehicles"),
    staleTime: 60_000,
    enabled,
  });
}
