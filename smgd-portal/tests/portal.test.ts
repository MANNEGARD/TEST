import { test } from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";

const SECRET = randomBytes(32).toString("base64");
const env: Record<string, string> = {
  PORTAL_PASSWORD_HASH: "pbkdf2-sha256$100000$AAAA$AAAA",
  PORTAL_TOTP_SECRET: "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
  PORTAL_SESSION_SECRET: SECRET,
};
(globalThis as any).Netlify = { env: { get: (k: string) => env[k] } };

const { createSessionToken, SESSION_COOKIE } = await import("../netlify/edge-functions/lib/security.ts");
const portal = (await import("../netlify/edge-functions/portal.ts")).default;

async function visit(token?: string) {
  const ctx = { ip: "127.0.0.1", cookies: { get: (n: string) => (n === SESSION_COOKIE ? token : undefined) } };
  return portal(new Request("https://smgd.se/"), ctx as any) as Promise<Response>;
}

test("utan kaka visas inloggningen", async () => {
  const res = await visit();
  assert.match(await res.text(), /Logga in/);
});

test("håll mig inloggad förnyas när kakan är äldre än ett dygn", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: Date.now() - 3 * 24 * 3600 * 1000 });
  const old = await createSessionToken(SECRET, "1", true);
  t.mock.timers.reset();
  const res = await visit(old.token);
  assert.match(await res.text(), /Vart vill du gå/);
  const cookie = res.headers.get("set-cookie") ?? "";
  assert.match(cookie, /Max-Age=3456000\d|Max-Age=34559\d\d\d/);
});

test("färsk håll-mig-inloggad-kaka förnyas inte i onödan", async () => {
  const fresh = await createSessionToken(SECRET, "1", true);
  const res = await visit(fresh.token);
  assert.equal(res.headers.get("set-cookie"), null);
});

test("vanlig session förnyas aldrig", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: Date.now() - 2 * 3600 * 1000 });
  const s = await createSessionToken(SECRET, "1");
  t.mock.timers.reset();
  const res = await visit(s.token);
  assert.match(await res.text(), /Vart vill du gå/);
  assert.equal(res.headers.get("set-cookie"), null);
});
