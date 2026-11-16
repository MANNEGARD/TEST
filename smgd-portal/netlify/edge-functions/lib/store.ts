// Var inloggningsuppgifterna finns: i Netlify Blobs, där installationssidan (/setup)
// sparar dem. Miljövariabler (PORTAL_*) går före om de finns, för den som hellre
// använder scripts/setup.mjs.
import { getStore } from "@netlify/blobs";
import { readAuthConfig, type AuthConfig } from "./security.ts";

const STORE = "smgd-portal-auth";
const CREDENTIALS_KEY = "credentials";
const PENDING_KEY = "setup-pending";

export interface StoredCredentials {
  passwordHash: string;
  totpSecret: string;
  sessionSecret: string;
  createdAt: string;
}

export interface PendingSetup {
  totpSecret: string;
  expires: number; // unix-ms
}

export function authStore() {
  return getStore({ name: STORE, consistency: "strong" });
}

let cache: { cfg: AuthConfig | null; at: number } | null = null;
const CACHE_MS = 30_000;

/** Returnerar inloggningsuppgifterna, eller null om ingen installation är gjord. */
export async function loadAuthConfig(): Promise<AuthConfig | null> {
  const fromEnv = readAuthConfig();
  if (fromEnv) return fromEnv;
  if (cache && Date.now() - cache.at < CACHE_MS) return cache.cfg;
  const stored = (await authStore().get(CREDENTIALS_KEY, { type: "json" })) as StoredCredentials | null;
  const cfg = stored
    ? {
        passwordHash: stored.passwordHash,
        totpSecret: stored.totpSecret,
        sessionSecret: stored.sessionSecret,
        sessionVersion: Netlify.env.get("PORTAL_SESSION_VERSION") ?? "1",
      }
    : null;
  cache = { cfg, at: Date.now() };
  return cfg;
}

export async function isSetUp(): Promise<boolean> {
  if (readAuthConfig()) return true;
  return (await authStore().getMetadata(CREDENTIALS_KEY)) !== null;
}

/** Sparar uppgifterna – bara om inga finns sedan tidigare. Returnerar false annars. */
export async function saveCredentials(creds: StoredCredentials): Promise<boolean> {
  const { modified } = await authStore().setJSON(CREDENTIALS_KEY, creds, { onlyIfNew: true });
  cache = null;
  return modified;
}

export async function savePending(p: PendingSetup): Promise<void> {
  await authStore().setJSON(PENDING_KEY, p);
}

export async function loadPending(): Promise<PendingSetup | null> {
  const p = (await authStore().get(PENDING_KEY, { type: "json" })) as PendingSetup | null;
  if (!p || p.expires < Date.now()) return null;
  return p;
}

// ---------- Spärr efter upprepade fel (per IP-adress) ----------

export const MAX_FAILURES = 5;
const FAILURE_WINDOW_MS = 15 * 60 * 1000;
export const LOCKOUT_MS = 15 * 60 * 1000;

interface Failures {
  count: number;
  first: number;
  lockedUntil: number;
}

async function failureKey(ip: string): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`smgd:${ip}`)));
  return "fail/" + Array.from(digest.slice(0, 12), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Hur länge till (ms) IP-adressen är spärrad, eller 0. */
export async function lockedFor(ip: string): Promise<number> {
  const f = (await authStore().get(await failureKey(ip), { type: "json" })) as Failures | null;
  return f && f.lockedUntil > Date.now() ? f.lockedUntil - Date.now() : 0;
}

export async function recordFailure(ip: string): Promise<boolean> {
  const key = await failureKey(ip);
  const store = authStore();
  const now = Date.now();
  let f = (await store.get(key, { type: "json" })) as Failures | null;
  if (!f || now - f.first > FAILURE_WINDOW_MS) f = { count: 0, first: now, lockedUntil: 0 };
  f.count += 1;
  if (f.count >= MAX_FAILURES) f.lockedUntil = now + LOCKOUT_MS;
  await store.setJSON(key, f);
  return f.lockedUntil > now;
}

export async function clearFailures(ip: string): Promise<void> {
  await authStore().delete(await failureKey(ip));
}

export async function clearPending(): Promise<void> {
  await authStore().delete(PENDING_KEY);
}
