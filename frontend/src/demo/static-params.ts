/**
 * Statiska sidor för demots dynamiska adresser (bara i demobygget, output "export").
 *
 * Varje resa och fråga i startdatat får en egen förbyggd sida, och dessutom en uppsättning
 * reserverade id:n som demot delar ut till resor och frågor som skapas under körning
 * (se poolTripId/poolQuestionId). Månadsvyn byggs för 2025–2027.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { POOL_SIZE, poolQuestionId, poolTripId } from "./state";

interface SeedIds {
  trips: Record<string, unknown>;
  notifications: Record<string, unknown>;
}

function seed(): SeedIds {
  return JSON.parse(readFileSync(path.join(process.cwd(), "demo-static", "startdata.json"), "utf8")) as SeedIds;
}

const pool = (make: (n: number) => string) => Array.from({ length: POOL_SIZE }, (_, i) => make(i));

export function journalParams(): { id: string }[] {
  return [...Object.keys(seed().trips), ...pool(poolTripId)].map((id) => ({ id }));
}

export function questionParams(): { id: string }[] {
  return [...Object.keys(seed().notifications), ...pool(poolQuestionId)].map((id) => ({ id }));
}

export function monthParams(): { month: string }[] {
  const out: { month: string }[] = [];
  for (const y of [2025, 2026, 2027]) for (let m = 1; m <= 12; m++) out.push({ month: `${y}-${String(m).padStart(2, "0")}` });
  return out;
}
