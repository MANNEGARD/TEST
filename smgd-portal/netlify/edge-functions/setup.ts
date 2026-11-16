// Engångsinstallation av inloggningen.
//
// GET  /setup              – installationssidan (404 när installationen redan är gjord)
// POST /api/setup/start    – kontrollerar engångsnyckeln och skapar en ny TOTP-nyckel + QR-kod
// POST /api/setup/finish   – sparar lösenord + TOTP-nyckel när första koden stämmer, och loggar in
//
// Engångsnyckeln sätts i miljövariabeln PORTAL_SETUP_TOKEN. När uppgifterna väl är sparade
// går installationen inte att köra igen, oavsett nyckel.
import type { Config, Context } from "@netlify/edge-functions";
import qrcode from "qrcode-generator";
import { setupPage } from "./lib/pages.ts";
import {
  base32Encode,
  createSessionToken,
  hashPassword,
  isSameOrigin,
  json,
  randomBase64,
  safeStringEqual,
  securityHeaders,
  sessionCookie,
  verifyTotp,
} from "./lib/security.ts";
import { authStore, clearPending, isSetUp, loadPending, saveCredentials, savePending } from "./lib/store.ts";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function notFound(): Response {
  return new Response("Hittades inte.", { status: 404, headers: securityHeaders({ "Content-Type": "text/plain; charset=utf-8" }) });
}

function tokenOk(given: unknown): boolean {
  const expected = Netlify.env.get("PORTAL_SETUP_TOKEN") ?? "";
  return expected.length >= 32 && typeof given === "string" && safeStringEqual(given, expected);
}

export default async (request: Request, context: Context) => {
  const url = new URL(request.url);

  if (await isSetUp()) return notFound();

  if (url.pathname === "/setup") {
    if (request.method !== "GET" && request.method !== "HEAD") return notFound();
    return new Response(setupPage(), { headers: securityHeaders({ "Content-Type": "text/html; charset=utf-8" }) });
  }

  if (request.method !== "POST" || !isSameOrigin(request)) return json({ error: "Ogiltig begäran." }, 403);

  const raw = await request.text();
  if (raw.length > 2048) return json({ error: "Ogiltig begäran." }, 413);
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: "Ogiltig begäran." }, 400);
  }

  if (!tokenOk(body.token)) {
    console.warn(`Felaktig installationsnyckel från ${context.ip}`);
    await sleep(800);
    return json({ error: "Installationslänken är ogiltig eller har redan använts." }, 403);
  }

  if (url.pathname === "/api/setup/start") {
    const totpSecret = base32Encode(crypto.getRandomValues(new Uint8Array(20)));
    await savePending({ totpSecret, expires: Date.now() + 15 * 60 * 1000 });
    const otpauth = `otpauth://totp/SMGD:smgd.se?secret=${totpSecret}&issuer=SMGD&algorithm=SHA1&digits=6&period=30`;
    const qr = qrcode(0, "M");
    qr.addData(otpauth);
    qr.make();
    return json({
      secret: totpSecret.match(/.{1,4}/g)!.join(" "),
      otpauth,
      qrSvg: qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true }),
    });
  }

  if (url.pathname === "/api/setup/finish") {
    const password = typeof body.password === "string" ? body.password : "";
    const code = typeof body.code === "string" ? body.code.replace(/\s/g, "") : "";
    if (password.length < 12 || password.length > 256) {
      return json({ error: "Lösenordet måste vara minst 12 tecken." }, 400);
    }
    const pending = await loadPending();
    if (!pending) return json({ error: "Installationen tog för lång tid. Ladda om sidan och börja om." }, 410);
    const step = await verifyTotp(code, pending.totpSecret);
    if (step === null) {
      await sleep(500);
      return json({ error: "Koden stämmer inte. Kontrollera att mobilens klocka går rätt och försök igen." }, 400);
    }

    const sessionSecret = randomBase64(32);
    const saved = await saveCredentials({
      passwordHash: await hashPassword(password),
      totpSecret: pending.totpSecret,
      sessionSecret,
      createdAt: new Date().toISOString(),
    });
    await clearPending();
    await authStore().set("totp-last-step", String(step)); // koden får inte återanvändas vid inloggning
    if (!saved) return json({ error: "Installationen är redan gjord." }, 409);

    console.log(`Inloggningen installerades från ${context.ip}`);
    const { token, session } = await createSessionToken(sessionSecret, Netlify.env.get("PORTAL_SESSION_VERSION") ?? "1");
    return json({ ok: true, expiresAt: session.exp }, 200, { "Set-Cookie": sessionCookie(token) });
  }

  return notFound();
};

export const config: Config = {
  path: ["/setup", "/api/setup/start", "/api/setup/finish"],
  rateLimit: { windowLimit: 10, windowSize: 60, aggregateBy: ["ip", "domain"] },
};
