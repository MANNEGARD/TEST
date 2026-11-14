import { ApiError, errorMessage } from "@/lib/api";
import { formatNumber, formatTime } from "@/lib/format";

/** "42,5" → 42.5 · tomt → null · ogiltigt → NaN (decimalkomma eller punkt, mellanslag tillåts). */
export function parseNumber(v: string): number | null {
  const s = v.replace(/\s/g, "").replace(",", ".");
  if (!s) return null;
  if (!/^\d+(\.\d+)?$/.test(s)) return NaN;
  return Number(s);
}

/** Tal → text i ett inmatningsfält (decimalkomma, inga tusentalsavgränsare). */
export function numberField(n: number | null | undefined, maxDecimals = 2): string {
  if (n == null) return "";
  return formatNumber(n, maxDecimals, 0).replace(/\s/g, "");
}

/** Aktuellt klockslag (HH:mm) i användarens tidszon. */
export function nowTime(): string {
  return formatTime(new Date());
}

/** Felmeddelande för formulär: backendens text, med fältnamn vid 422. */
export function formError(err: unknown): string {
  if (err instanceof ApiError && err.fields.length) {
    const first = err.fields[0];
    return first.message ? `${err.detail} ${first.message}` : err.detail;
  }
  return errorMessage(err);
}

export function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
