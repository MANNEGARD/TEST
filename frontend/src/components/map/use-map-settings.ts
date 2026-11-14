"use client";

import { useSettings } from "@/lib/queries/settings";

/**
 * Kartinställningar från GET /settings → settings.privacy. Kartplattor är det
 * enda externa anropet klienten gör; map_enabled=false visar platshållare.
 */
export function useMapSettings(): { enabled: boolean; tilesUrl: string | null; loading: boolean } {
  const { data, isLoading } = useSettings();
  const privacy = data?.settings.privacy;
  return {
    enabled: !!privacy?.map_enabled && !!privacy?.map_tiles_url,
    tilesUrl: privacy?.map_tiles_url ?? null,
    loading: isLoading,
  };
}
