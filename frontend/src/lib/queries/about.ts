"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { AboutInfo } from "../types";
import { useMe } from "./auth";

export const aboutKeys = {
  info: ["about"] as const,
};

/** Vilken version av DriveLog som körs och vad som är nytt i den (GET /about, bara inloggad). */
export function useAbout() {
  const me = useMe();
  return useQuery({
    queryKey: aboutKeys.info,
    queryFn: ({ signal }) => api.get<AboutInfo>("/about", { signal }),
    enabled: !!me.data,
    // Versionen byts bara när servern startas om med en ny avbild.
    staleTime: 30 * 60_000,
    retry: 1,
  });
}

/** "DriveLog 1.3.0", eller "DriveLog demo" i demot. */
export function versionLabel(version: string): string {
  return `DriveLog ${version}`;
}
