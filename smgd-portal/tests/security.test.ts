import { test } from "node:test";
import assert from "node:assert/strict";
import { pbkdf2Sync, randomBytes } from "node:crypto";
import {
  base32Decode,
  createSessionToken,
  isSameOrigin,
  readAuthConfig,
  readSessionToken,
  verifyPassword,
  verifyTotp,
} from "../netlify/edge-functions/lib/security.ts";

const RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"; // "12345678901234567890"

test("base32 avkodas korrekt", () => {
  assert.equal(new TextDecoder().decode(base32Decode(RFC_SECRET)), "12345678901234567890");
});

test("TOTP enligt RFC 6238-testvektorer", async () => {
  assert.equal(await verifyTotp("287082", RFC_SECRET, 59_000), 1);
  assert.equal(await verifyTotp("081804", RFC_SECRET, 1_111_111_109_000), 37_037_036);
  assert.equal(await verifyTotp("005924", RFC_SECRET, 1_234_567_890_000), 41_152_263);
});

test("TOTP tillåter ±1 steg men inte mer", async () => {
  const t = 1_111_111_109_000;
  assert.notEqual(await verifyTotp("081804", RFC_SECRET, t + 30_000), null);
  assert.notEqual(await verifyTotp("081804", RFC_SECRET, t - 30_000), null);
  assert.equal(await verifyTotp("081804", RFC_SECRET, t + 61_000), null);
});

test("TOTP avvisar felaktigt format", async () => {
  for (const bad of ["", "12345", "1234567", "abcdef", "12 345"]) {
    assert.equal(await verifyTotp(bad, RFC_SECRET, 59_000), null);
  }
});

function makeHash(password: string, iterations = 100_000): string {
  const salt = randomBytes(16);
  const hash = pbkdf2Sync(password.normalize("NFC"), salt, iterations, 32, "sha256");
  return `pbkdf2-sha256$${iterations}$${salt.toString("base64")}$${hash.toString("base64")}`;
}

test("lösenord: hash från setup-skriptet godkänns", async () => {
  const stored = makeHash("korrekt häst batteri häftapparat");
  assert.equal(await verifyPassword("korrekt häst batteri häftapparat", stored), true);
  assert.equal(await verifyPassword("korrekt häst batteri häftapparaT", stored), false);
  assert.equal(await verifyPassword("", stored), false);
});

test("lösenord: ogiltiga hashformat avvisas", async () => {
  assert.equal(await verifyPassword("x", "plaintext"), false);
  assert.equal(await verifyPassword("x", makeHash("x", 1000)), false); // för få iterationer
  assert.equal(await verifyPassword("x", "bcrypt$1$2$3"), false);
});

const SECRET = randomBytes(32).toString("base64");

test("session: giltig token läses tillbaka", async () => {
  const { token, session } = await createSessionToken(SECRET, "1");
  const read = await readSessionToken(token, SECRET, "1");
  assert.deepEqual(read, session);
});

test("session: manipulerad token, fel nyckel eller fel version avvisas", async () => {
  const { token } = await createSessionToken(SECRET, "1");
  const [v, payload, sig] = token.split(".");
  const forged = JSON.parse(Buffer.from(payload, "base64url").toString());
  forged.exp += 999_999;
  const forgedPayload = Buffer.from(JSON.stringify(forged)).toString("base64url");
  assert.equal(await readSessionToken(`${v}.${forgedPayload}.${sig}`, SECRET, "1"), null);
  assert.equal(await readSessionToken(token, randomBytes(32).toString("base64"), "1"), null);
  assert.equal(await readSessionToken(token, SECRET, "2"), null);
  assert.equal(await readSessionToken(undefined, SECRET, "1"), null);
  assert.equal(await readSessionToken("skräp", SECRET, "1"), null);
});

test("session: utgången token avvisas", async (t) => {
  const { token } = await createSessionToken(SECRET, "1");
  t.mock.timers.enable({ apis: ["Date"], now: Date.now() + 13 * 3600 * 1000 });
  assert.equal(await readSessionToken(token, SECRET, "1"), null);
});

test("CSRF: kräver samma ursprung", () => {
  const mk = (origin?: string) =>
    new Request("https://smgd.se/api/login", { method: "POST", headers: origin ? { origin } : {} });
  assert.equal(isSameOrigin(mk("https://smgd.se")), true);
  assert.equal(isSameOrigin(mk("https://evil.example")), false);
  assert.equal(isSameOrigin(mk("https://dev.smgd.se")), false);
  assert.equal(isSameOrigin(mk()), false);
});

test("konfiguration: saknade hemligheter ger null (stängt läge)", () => {
  const env = new Map<string, string>();
  (globalThis as any).Netlify = { env: { get: (k: string) => env.get(k) } };
  assert.equal(readAuthConfig(), null);
  env.set("PORTAL_PASSWORD_HASH", makeHash("x"));
  env.set("PORTAL_TOTP_SECRET", RFC_SECRET);
  assert.equal(readAuthConfig(), null);
  env.set("PORTAL_SESSION_SECRET", SECRET);
  assert.notEqual(readAuthConfig(), null);
});

test("håll mig inloggad: 400 dagar och beständig kaka, annars sessionskaka", async () => {
  const { sessionCookie } = await import("../netlify/edge-functions/lib/security.ts");
  const kort = await createSessionToken(SECRET, "1");
  const lang = await createSessionToken(SECRET, "1", true);
  assert.equal(kort.session.exp - kort.session.iat, 12 * 3600);
  assert.equal(lang.session.exp - lang.session.iat, 400 * 24 * 3600);
  assert.doesNotMatch(sessionCookie(kort.token, kort.session), /Max-Age/);
  assert.match(sessionCookie(lang.token, lang.session), /Max-Age=34560000/);
  assert.match(sessionCookie(lang.token, lang.session), /HttpOnly; Secure; SameSite=Lax/);
  assert.notEqual(await readSessionToken(lang.token, SECRET, "1"), null);
});
