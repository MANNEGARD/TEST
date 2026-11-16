// POST /api/login – kontrollerar lösenord + engångskod och sätter sessionskakan.
import type { Config, Context } from "@netlify/edge-functions";
import { getStore } from "@netlify/blobs";
import {
  createSessionToken,
  isSameOrigin,
  json,
  readAuthConfig,
  sessionCookie,
  verifyPassword,
  verifyTotp,
} from "./lib/security.ts";

const GENERIC_ERROR = "Fel lösenord eller engångskod.";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Senaste använda TOTP-steg sparas så att samma kod inte kan användas två gånger. */
async function claimTotpStep(step: number): Promise<boolean> {
  try {
    const store = getStore({ name: "smgd-portal-auth", consistency: "strong" });
    const last = Number((await store.get("totp-last-step")) ?? "0");
    if (step <= last) return false;
    await store.set("totp-last-step", String(step));
    return true;
  } catch (err) {
    // Lagringen ska inte kunna låsa ute ägaren; koden är ändå bara giltig i ±30 s.
    console.warn("TOTP-återanvändningskontroll otillgänglig:", err);
    return true;
  }
}

export default async (request: Request, context: Context) => {
  if (request.method !== "POST") return json({ error: "Metoden stöds inte." }, 405, { Allow: "POST" });
  if (!isSameOrigin(request)) return json({ error: "Ogiltig begäran." }, 403);
  if (!(request.headers.get("content-type") ?? "").startsWith("application/json")) {
    return json({ error: "Ogiltig begäran." }, 415);
  }

  const cfg = readAuthConfig();
  if (!cfg) {
    console.error("Portalen saknar PORTAL_PASSWORD_HASH, PORTAL_TOTP_SECRET eller PORTAL_SESSION_SECRET.");
    return json({ error: "Inloggningen är inte konfigurerad ännu." }, 503);
  }

  const raw = await request.text();
  if (raw.length > 2048) return json({ error: "Ogiltig begäran." }, 413);

  let password = "";
  let code = "";
  try {
    const body = JSON.parse(raw);
    password = typeof body.password === "string" ? body.password : "";
    code = typeof body.code === "string" ? body.code.replace(/\s/g, "") : "";
  } catch {
    return json({ error: "Ogiltig begäran." }, 400);
  }

  // Kontrollera båda faktorerna alltid, så att svaret inte avslöjar vilken som var fel.
  const passwordOk = password.length > 0 && password.length <= 256 && (await verifyPassword(password, cfg.passwordHash));
  const step = await verifyTotp(code, cfg.totpSecret);

  if (!passwordOk || step === null || !(await claimTotpStep(step))) {
    console.warn(`Misslyckad inloggning från ${context.ip}`);
    await sleep(700 + Math.floor(Math.random() * 500)); // bromsar gissningar (väntan, inte CPU)
    return json({ error: GENERIC_ERROR }, 401);
  }

  const { token, session } = await createSessionToken(cfg.sessionSecret, cfg.sessionVersion);
  console.log(`Lyckad inloggning från ${context.ip}`);
  return json({ ok: true, expiresAt: session.exp }, 200, { "Set-Cookie": sessionCookie(token) });
};

export const config: Config = {
  path: "/api/login",
  method: "POST",
  // Netlifys inbyggda hastighetsbegränsning: högst 8 försök per minut och IP-adress.
  rateLimit: {
    windowLimit: 8,
    windowSize: 60,
    aggregateBy: ["ip", "domain"],
  },
};
