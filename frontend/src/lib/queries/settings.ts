"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { SettingsResponse } from "../types";
import { qk } from "./keys";

export function useSettings(enabled = true) {
  return useQuery({
    queryKey: qk.settings,
    queryFn: () => api.get<SettingsResponse>("/settings"),
    staleTime: 5 * 60_000,
    enabled,
  });
}
