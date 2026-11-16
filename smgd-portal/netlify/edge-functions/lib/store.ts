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

export async function clearPending(): Promise<void> {
  await authStore().delete(PENDING_KEY);
}
