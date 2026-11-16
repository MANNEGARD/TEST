// Kryptografiska byggstenar för portalen: lösenordskontroll (PBKDF2), TOTP (RFC 6238)
// och signerade sessionskakor (HMAC-SHA256). Endast Web Crypto används – inga beroenden.

const enc = new TextEncoder();

export const SESSION_COOKIE = "__Host-smgd_session";
export const SESSION_TTL_SECONDS = 12 * 60 * 60; // 12 timmar, därefter krävs ny inloggning

// ---------- Hjälpfunktioner ----------

export function b64urlEncode(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64Decode(input: string): Uint8Array {
  const norm = input.replace(/-/g, "+").replace(/_/g, "/");
  const padded = norm + "=".repeat((4 - (norm.length % 4)) % 4);
  const bin = atob(padded);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Jämförelse i konstant tid, så att svarstiden inte avslöjar hur många bytes som stämde. */
export function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  const len = Math.max(a.length, b.length);
  let diff = a.length ^ b.length;
  for (let i = 0; i < len; i++) diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  return diff === 0;
}

async function hmac(hash: "SHA-1" | "SHA-256", key: Uint8Array, data: Uint8Array): Promise<Uint8Array> {
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
}

// ---------- Lösenord ----------

/**
 * Kontrollerar ett lösenord mot en hash i formatet
 * `pbkdf2-sha256$<iterationer>$<salt base64>$<hash base64>` (skapas av scripts/setup.mjs).
 */
export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const parts = stored.split("$");
  if (parts.length !== 4 || parts[0] !== "pbkdf2-sha256") return false;
  const iterations = Number(parts[1]);
  if (!Number.isInteger(iterations) || iterations < 50_000 || iterations > 1_000_000) return false;
  const salt = b64Decode(parts[2]);
  const expected = b64Decode(parts[3]);
  const key = await crypto.subtle.importKey("raw", enc.encode(password.normalize("NFC")), "PBKDF2", false, [
    "deriveBits",
  ]);
  const derived = new Uint8Array(
    await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, key, expected.length * 8),
  );
  return timingSafeEqual(derived, expected);
}

export const PASSWORD_ITERATIONS = 100_000; // ryms inom edge-funktionernas CPU-gräns

/** Skapar en hash i samma format som verifyPassword läser. */
export async function hashPassword(password: string): Promise<string> {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey("raw", enc.encode(password.normalize("NFC")), "PBKDF2", false, [
    "deriveBits",
  ]);
  const hash = new Uint8Array(
    await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: PASSWORD_ITERATIONS }, key, 256),
  );
  const b64 = (u: Uint8Array) => btoa(String.fromCharCode(...u));
  return `pbkdf2-sha256$${PASSWORD_ITERATIONS}$${b64(salt)}$${b64(hash)}`;
}

export function randomBase64(bytes: number): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes))));
}

export function base32Encode(buf: Uint8Array): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  let out = "";
  for (const b of buf) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += alphabet[(value << (5 - bits)) & 31];
  return out;
}

export function safeStringEqual(a: string, b: string): boolean {
  return timingSafeEqual(enc.encode(a), enc.encode(b));
}

// ---------- TOTP (RFC 6238, SHA-1, 6 siffror, 30 s) ----------

export function base32Decode(input: string): Uint8Array {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = input.toUpperCase().replace(/[\s=-]/g, "");
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const ch of clean) {
    const idx = alphabet.indexOf(ch);
    if (idx === -1) throw new Error("Ogiltig base32");
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(out);
}

async function hotp(secret: Uint8Array, counter: number): Promise<string> {
  const buf = new Uint8Array(8);
  let c = counter;
  for (let i = 7; i >= 0; i--) {
    buf[i] = c & 0xff;
    c = Math.floor(c / 256);
  }
  const mac = await hmac("SHA-1", secret, buf);
  const offset = mac[mac.length - 1] & 0x0f;
  const bin =
    ((mac[offset] & 0x7f) << 24) | (mac[offset + 1] << 16) | (mac[offset + 2] << 8) | mac[offset + 3];
  return (bin % 1_000_000).toString().padStart(6, "0");
}

/**
 * Returnerar tidssteget som koden gällde för (för att kunna stoppa återanvändning),
 * eller null om koden är fel. Tillåter ±1 steg (±30 s) för klockdrift.
 */
export async function verifyTotp(code: string, base32Secret: string, now = Date.now()): Promise<number | null> {
  if (!/^\d{6}$/.test(code)) return null;
  const secret = base32Decode(base32Secret);
  const step = Math.floor(now / 1000 / 30);
  let match: number | null = null;
  // Gå igenom alla fönster oavsett träff, så att tidsåtgången blir densamma.
  for (const s of [step - 1, step, step + 1]) {
    const candidate = await hotp(secret, s);
    if (timingSafeEqual(enc.encode(candidate), enc.encode(code)) && match === null) match = s;
  }
  return match;
}

// ---------- Session ----------

export interface Session {
  iat: number; // utfärdad (unix-sekunder)
  exp: number; // går ut (unix-sekunder)
  ver: string; // sessionsversion – byt PORTAL_SESSION_VERSION för att logga ut alla
  jti: string; // slumpat id
}

export async function createSessionToken(secret: string, version: string): Promise<{ token: string; session: Session }> {
  const now = Math.floor(Date.now() / 1000);
  const session: Session = {
    iat: now,
    exp: now + SESSION_TTL_SECONDS,
    ver: version,
    jti: b64urlEncode(crypto.getRandomValues(new Uint8Array(12))),
  };
  const payload = b64urlEncode(enc.encode(JSON.stringify(session)));
  const sig = b64urlEncode(await hmac("SHA-256", b64Decode(secret), enc.encode(`v1.${payload}`)));
  return { token: `v1.${payload}.${sig}`, session };
}

export async function readSessionToken(
  token: string | undefined,
  secret: string,
  version: string,
): Promise<Session | null> {
  if (!token || token.length > 1024) return null;
  const parts = token.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") return null;
  try {
    const expected = await hmac("SHA-256", b64Decode(secret), enc.encode(`v1.${parts[1]}`));
    if (!timingSafeEqual(expected, b64Decode(parts[2]))) return null;
    const session = JSON.parse(new TextDecoder().decode(b64Decode(parts[1]))) as Session;
    const now = Math.floor(Date.now() / 1000);
    if (typeof session.exp !== "number" || session.exp <= now) return null;
    if (typeof session.iat !== "number" || session.iat > now + 60) return null;
    if (session.ver !== version) return null;
    return session;
  } catch {
    return null;
  }
}

// ---------- Konfiguration ----------

export interface AuthConfig {
  passwordHash: string;
  totpSecret: string;
  sessionSecret: string;
  sessionVersion: string;
}

/** Läser hemligheterna. Saknas något returneras null – och då går det inte att logga in alls. */
export function readAuthConfig(): AuthConfig | null {
  const passwordHash = Netlify.env.get("PORTAL_PASSWORD_HASH") ?? "";
  const totpSecret = Netlify.env.get("PORTAL_TOTP_SECRET") ?? "";
  const sessionSecret = Netlify.env.get("PORTAL_SESSION_SECRET") ?? "";
  const sessionVersion = Netlify.env.get("PORTAL_SESSION_VERSION") ?? "1";
  if (!passwordHash.startsWith("pbkdf2-sha256$") || totpSecret.length < 16 || sessionSecret.length < 40) return null;
  return { passwordHash, totpSecret, sessionSecret, sessionVersion };
}

export function sessionCookie(token: string): string {
  return `${SESSION_COOKIE}=${token}; Path=/; Max-Age=${SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax`;
}

export function clearSessionCookie(): string {
  return `${SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`;
}

/** Skydd mot CSRF: POST måste komma från samma ursprung som sidan. */
export function isSameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  return origin === new URL(request.url).origin;
}

export function securityHeaders(extra: Record<string, string> = {}): Headers {
  const h = new Headers({
    "Content-Security-Policy":
      "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; " +
      "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; " +
      "upgrade-insecure-requests",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
  });
  for (const [k, v] of Object.entries(extra)) h.set(k, v);
  return h;
}

export function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: securityHeaders({ "Content-Type": "application/json; charset=utf-8", ...extra }),
  });
}
