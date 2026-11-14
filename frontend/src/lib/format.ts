/**
 * Central formatering (svensk stil). ALL visning av datum, tider, sträckor och
 * mängder ska gå via dessa funktioner.
 *
 *  - Datum yyyy-MM-dd, tid HH:mm, i användarens tidszon (settings.general.timezone).
 *    Tidszonen sätts av app-skalet med `setTimeZone()` när inställningarna laddats.
 *  - Decimalkomma och mellanslag som tusentalsavgränsare (sv-SE).
 *  - Sträcka: mil primärt i nyckeltal (`formatMil`), km i tabeller (`formatKm`).
 *  - Funktionerna returnerar "–" för saknade värden. Vill du visa att ett värde
 *    saknas/är uppskattat: använd komponenten <QualityValue>.
 */

export const MISSING = "–";

let timeZone = "Europe/Stockholm";

export function setTimeZone(tz: string | null | undefined): void {
  if (!tz) return;
  try {
    new Intl.DateTimeFormat("sv-SE", { timeZone: tz });
    timeZone = tz;
  } catch {
    /* ogiltig tidszon – behåll föregående */
  }
}

export function getTimeZone(): string {
  return timeZone;
}

type DateInput = string | number | Date | null | undefined;

function toDate(value: DateInput): Date | null {
  if (value == null || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

const dtfCache = new Map<string, Intl.DateTimeFormat>();
function dtf(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(options)}`;
  let f = dtfCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("sv-SE", { timeZone, ...options });
    dtfCache.set(key, f);
  }
  return f;
}

function parts(d: Date, options: Intl.DateTimeFormatOptions): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of dtf(options).formatToParts(d)) out[p.type] = p.value;
  return out;
}

/** 2026-07-20 */
export function formatDate(value: DateInput): string {
  const d = toDate(value);
  if (!d) return MISSING;
  const p = parts(d, { year: "numeric", month: "2-digit", day: "2-digit" });
  return `${p.year}-${p.month}-${p.day}`;
}

/** 13:28 */
export function formatTime(value: DateInput): string {
  const d = toDate(value);
  if (!d) return MISSING;
  const p = parts(d, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return `${p.hour}:${p.minute}`;
}

/** 2026-07-20 13:28 */
export function formatDateTime(value: DateInput): string {
  const d = toDate(value);
  if (!d) return MISSING;
  return `${formatDate(d)} ${formatTime(d)}`;
}

const WEEKDAYS = ["söndag", "måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag"];
const WEEKDAYS_SHORT = ["sön", "mån", "tis", "ons", "tor", "fre", "lör"];
const MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"];
const MONTHS_SHORT = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"];

function weekdayIndex(d: Date): number {
  const wd = parts(d, { weekday: "short" }).weekday?.toLowerCase().replace(".", "") ?? "";
  const i = WEEKDAYS_SHORT.findIndex((w) => wd.startsWith(w));
  return i >= 0 ? i : d.getDay();
}

/** "måndag" */
export function formatWeekday(value: DateInput, short = false): string {
  const d = toDate(value);
  if (!d) return MISSING;
  const i = weekdayIndex(d);
  return short ? WEEKDAYS_SHORT[i] : WEEKDAYS[i];
}

/** "mån 2026-07-20" */
export function formatDateWithWeekday(value: DateInput): string {
  const d = toDate(value);
  if (!d) return MISSING;
  return `${formatWeekday(d, true)} ${formatDate(d)}`;
}

export function monthName(month1: number, short = false): string {
  return (short ? MONTHS_SHORT : MONTHS)[(month1 - 1 + 12) % 12];
}

/** Lokalt datum (yyyy-MM-dd) i användarens tidszon för ett ögonblick, standard nu. */
export function localDateISO(value: DateInput = new Date()): string {
  return formatDate(value);
}

/** "för 5 min sedan", "i går 14:02", "2026-07-20" */
export function formatRelative(value: DateInput, now: Date = new Date()): string {
  const d = toDate(value);
  if (!d) return MISSING;
  const diffSec = Math.round((now.getTime() - d.getTime()) / 1000);
  if (diffSec < 0) return formatDateTime(d);
  if (diffSec < 60) return "nyss";
  if (diffSec < 3600) return `för ${Math.floor(diffSec / 60)} min sedan`;
  if (formatDate(d) === formatDate(now)) return `i dag ${formatTime(d)}`;
  const yesterday = new Date(now.getTime() - 86400_000);
  if (formatDate(d) === formatDate(yesterday)) return `i går ${formatTime(d)}`;
  if (diffSec < 7 * 86400) return `${formatWeekday(d)} ${formatTime(d)}`;
  return formatDate(d);
}

// ----------------------------------------------------------------------------- tal

const nfCache = new Map<string, Intl.NumberFormat>();
function nf(min: number, max: number): Intl.NumberFormat {
  const key = `${min}|${max}`;
  let f = nfCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat("sv-SE", { minimumFractionDigits: min, maximumFractionDigits: max });
    nfCache.set(key, f);
  }
  return f;
}

/** 1 234,5 – `decimals` fasta decimaler (standard 0–1 efter behov). */
export function formatNumber(value: number | null | undefined, decimals?: number, minDecimals?: number): string {
  if (value == null || Number.isNaN(value)) return MISSING;
  if (decimals === undefined) return nf(minDecimals ?? 0, 1).format(value);
  return nf(minDecimals ?? decimals, decimals).format(value);
}

/** 38,9 km */
export function formatKm(km: number | null | undefined, decimals = 1): string {
  if (km == null) return MISSING;
  return `${formatNumber(km, decimals)} km`;
}

/** 3,9 mil (indata i km) */
export function formatMil(km: number | null | undefined, decimals = 1): string {
  if (km == null) return MISSING;
  return `${formatNumber(km / 10, decimals)} mil`;
}

/** Endast talet i mil, utan enhet (för nyckeltal där enheten visas separat). */
export function milNumber(km: number | null | undefined, decimals = 1): string {
  if (km == null) return MISSING;
  return formatNumber(km / 10, decimals);
}

/** 182 991 km – mätarställning (heltal). */
export function formatOdometer(km: number | null | undefined): string {
  if (km == null) return MISSING;
  return `${formatNumber(Math.round(km), 0)} km`;
}

/** 2,9 l */
export function formatLiters(l: number | null | undefined, decimals = 1): string {
  if (l == null) return MISSING;
  return `${formatNumber(l, decimals)} l`;
}

/** 4,3 kWh */
export function formatKwh(kwh: number | null | undefined, decimals = 1): string {
  if (kwh == null) return MISSING;
  return `${formatNumber(kwh, decimals)} kWh`;
}

/** 7,2 l/100 km  eller  11,0 kWh/100 km */
export function formatPer100(value: number | null | undefined, unit: "l" | "kWh", decimals = 1): string {
  if (value == null) return MISSING;
  return `${formatNumber(value, decimals, decimals)} ${unit}/100 km`;
}

/** 1 h 52 min, 24 min */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return MISSING;
  const total = Math.max(0, Math.round(seconds / 60));
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (h === 0) return `${m} min`;
  if (m === 0) return `${h} h`;
  return `${h} h ${m} min`;
}

/** 1 234 kr */
export function formatCurrency(value: number | null | undefined, currency = "SEK"): string {
  if (value == null) return MISSING;
  const unit = currency === "SEK" ? "kr" : currency;
  return `${formatNumber(value, value >= 1000 ? 0 : 2, 0)} ${unit}`;
}

/** 0.874 → 87 % */
export function formatPercent(fraction: number | null | undefined, decimals = 0): string {
  if (fraction == null || Number.isNaN(fraction)) return MISSING;
  return `${formatNumber(fraction * 100, decimals, decimals)} %`;
}

/** Förändring med tecken: +12 % / −4 % */
export function formatChange(fraction: number | null | undefined): string {
  if (fraction == null) return MISSING;
  const pct = Math.round(fraction * 100);
  if (pct === 0) return "±0 %";
  return `${pct > 0 ? "+" : "−"}${formatNumber(Math.abs(pct), 0)} %`;
}

/** Antal med rätt böjning: plural(3, "resa", "resor") → "3 resor" */
export function plural(n: number, one: string, many: string): string {
  return `${formatNumber(n, 0)} ${n === 1 ? one : many}`;
}

// ----------------------------------------------------------------------------- indata

function tzOffsetMs(utcMs: number): number {
  const p = parts(new Date(utcMs), {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  const asUtc = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour), Number(p.minute), Number(p.second));
  return asUtc - utcMs;
}

/**
 * Lokalt datum + klockslag i användarens tidszon → ISO-8601 i UTC (med "Z").
 * Används när användaren matar in tider (backend kräver tidszon).
 */
export function zonedToISO(dateISO: string, time: string): string {
  const [y, m, d] = dateISO.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  let utc = guess - tzOffsetMs(guess);
  utc = guess - tzOffsetMs(utc); // korrigera över sommartidsskifte
  return new Date(utc).toISOString();
}
