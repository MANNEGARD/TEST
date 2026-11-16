/**
 * Kartbildernas anrop (MapLibre). Sidan skickar `Referrer-Policy: same-origin`, så utan
 * undantag får kartservern ingen Referer – och OpenStreetMaps kartservrar kräver en giltig
 * Referer (annars "403 Access blocked"). Bara anropen efter kartbilder får därför policyn
 * `strict-origin-when-cross-origin`: kartservern ser sidans ursprung
 * (t.ex. `https://drivelog.example/`), aldrig sökvägen. Allt annat följer sidans policy.
 */
import type { RequestParameters, ResourceType } from "maplibre-gl";

export const TILE_REFERRER_POLICY: ReferrerPolicy = "strict-origin-when-cross-origin";

/** `transformRequest` för MapLibre: kartbilder bär sidans ursprung som Referer. */
export function tileRequest(url: string, type?: ResourceType): RequestParameters {
  return (type as string | undefined) === "Tile" ? { url, referrerPolicy: TILE_REFERRER_POLICY } : { url };
}

/** Källhänvisning för kartbilderna (krävs av OpenStreetMap). */
export function attributionFor(url: string): string {
  if (url.includes("openstreetmap")) return '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
  return "";
}
