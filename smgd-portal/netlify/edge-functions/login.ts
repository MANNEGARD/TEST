// POST /api/login – kontrollerar lösenord + engångskod och sätter sessionskakan.
import type { Config, Context } from "@netlify/edge-functions";
import {
  createSessionToken,
  isSameOrigin,
  json,
  sessionCookie,
  verifyPassword,
  verifyTotp,
} from "./lib/security.ts";
import {
  authStore,
  clearFailures,
  loadAuthConfig,
  LOCKOUT_MS,
  lockedFor,
  recordFailure,
} from "./lib/store.ts";

const GENERIC_ERROR = "Fel lösenord eller engångskod.";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Senaste använda TOTP-steg sparas så att samma kod inte kan användas två gånger. */
async function claimTotpStep(step: number): Promise<boolean> {
  try {
    const store = authStore();
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

  const cfg = await loadAuthConfig();
  if (!cfg) {
    console.error("Inloggningen är inte installerad – öppna /setup med installationslänken.");
    return json({ error: "Inloggningen är inte installerad ännu." }, 503);
  }

  const lockMs = await lockedFor(context.ip);
  if (lockMs > 0) {
    const min = Math.ceil(lockMs / 60_000);
    return json({ error: `För många misslyckade försök. Försök igen om ${min} min.` }, 429, {
      "Retry-After": String(Math.ceil(lockMs / 1000)),
    });
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
    const nowLocked = await recordFailure(context.ip);
    await sleep(700 + Math.floor(Math.random() * 500)); // bromsar gissningar (väntan, inte CPU)
    if (nowLocked) {
      return json({ error: `För många misslyckade försök. Försök igen om ${LOCKOUT_MS / 60_000} min.` }, 429);
    }
    return json({ error: GENERIC_ERROR }, 401);
  }

  await clearFailures(context.ip);

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
