// Inloggning: lösenord hashas med scrypt, sessioner är slumpade tokens som bara lagras
// hashade i databasen. Konton skapas enbart via inbjudningslänkar från en administratör.
import crypto from "node:crypto";
import { promisify } from "node:util";
import type { FastifyReply, FastifyRequest } from "fastify";
import { config } from "./config.js";
import { db, now, type Role, type UserRow } from "./db.js";

const scrypt = promisify(crypto.scrypt) as (
  pw: crypto.BinaryLike,
  salt: crypto.BinaryLike,
  keylen: number,
  opts: crypto.ScryptOptions,
) => Promise<Buffer>;

const SCRYPT = { N: 1 << 15, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };
export const SESSION_COOKIE = "ts_session";

export async function hashPassword(password: string): Promise<string> {
  const salt = crypto.randomBytes(16);
  const key = await scrypt(password, salt, 32, SCRYPT);
  return `scrypt$${SCRYPT.N}$${SCRYPT.r}$${SCRYPT.p}$${salt.toString("base64")}$${key.toString("base64")}`;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const parts = stored.split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return false;
  const [, n, r, p, saltB64, keyB64] = parts as [string, string, string, string, string, string];
  const expected = Buffer.from(keyB64, "base64");
  const key = await scrypt(password, Buffer.from(saltB64, "base64"), expected.length, {
    N: Number(n),
    r: Number(r),
    p: Number(p),
    maxmem: SCRYPT.maxmem,
  });
  return key.length === expected.length && crypto.timingSafeEqual(key, expected);
}

// Används för att jämna ut svarstiden när e-postadressen inte finns.
let dummyHash: Promise<string> | null = null;
export function dummyPasswordHash(): Promise<string> {
  dummyHash ??= hashPassword(crypto.randomBytes(16).toString("hex"));
  return dummyHash;
}

export function validatePassword(password: unknown): string | null {
  if (typeof password !== "string") return "Lösenord saknas.";
  if (password.length < 10) return "Lösenordet måste ha minst 10 tecken.";
  if (password.length > 200) return "Lösenordet är för långt.";
  return null;
}

export function validateEmail(email: unknown): string | null {
  if (typeof email !== "string" || email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return "Ange en giltig e-postadress.";
  }
  return null;
}

export function sha256(token: string): string {
  return crypto.createHash("sha256").update(token).digest("hex");
}

export function randomToken(bytes = 32): string {
  return crypto.randomBytes(bytes).toString("base64url");
}

export function createSession(reply: FastifyReply, userId: number, userAgent: string): void {
  const token = randomToken();
  const expires = new Date(Date.now() + config.sessionDays * 86400_000);
  db.prepare("INSERT INTO sessions (token_hash, user_id, expires_at, user_agent) VALUES (?, ?, ?, ?)").run(
    sha256(token),
    userId,
    expires.toISOString(),
    userAgent.slice(0, 200),
  );
  reply.setCookie(SESSION_COOKIE, token, {
    httpOnly: true,
    secure: config.secureCookies,
    sameSite: "lax",
    path: "/",
    expires,
  });
}

export function destroySession(req: FastifyRequest, reply: FastifyReply): void {
  const token = req.cookies[SESSION_COOKIE];
  if (token) db.prepare("DELETE FROM sessions WHERE token_hash = ?").run(sha256(token));
  reply.clearCookie(SESSION_COOKIE, { path: "/" });
}

export type SessionUser = Pick<UserRow, "id" | "email" | "name" | "role">;

export function userFromRequest(req: FastifyRequest): SessionUser | null {
  const token = req.cookies[SESSION_COOKIE];
  if (!token || token.length > 100) return null;
  const row = db
    .prepare(
      `SELECT u.id, u.email, u.name, u.role FROM sessions s JOIN users u ON u.id = s.user_id
       WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0`,
    )
    .get(sha256(token), now()) as SessionUser | undefined;
  return row ?? null;
}

declare module "fastify" {
  interface FastifyRequest {
    user: SessionUser | null;
  }
}

export async function requireUser(req: FastifyRequest, reply: FastifyReply): Promise<void> {
  if (!req.user) {
    await reply.code(401).send({ error: "Du är inte inloggad." });
  }
}

export async function requireAdmin(req: FastifyRequest, reply: FastifyReply): Promise<void> {
  if (!req.user) {
    await reply.code(401).send({ error: "Du är inte inloggad." });
  } else if (req.user.role !== "admin") {
    await reply.code(403).send({ error: "Endast administratörer." });
  }
}

/**
 * Skydd mot CSRF: ändrande anrop måste komma från vår egen sida. Cookien är SameSite=Lax,
 * och dessutom kräver vi att Origin (när den skickas) matchar tjänstens adress.
 */
export function sameOrigin(req: FastifyRequest): boolean {
  const origin = req.headers.origin;
  if (!origin) {
    // Webbläsare skickar alltid Origin på POST/PATCH/DELETE och WebSocket; saknas den är
    // det ett skript (curl o.d.) utan våra cookies – tillåt bara om headern X-Requested-With finns.
    return req.headers["x-requested-with"] === "transcribe";
  }
  const allowed = new Set([config.publicUrl]);
  const host = req.headers.host;
  if (host) {
    allowed.add(`https://${host}`);
    if (!config.secureCookies) allowed.add(`http://${host}`);
  }
  return allowed.has(origin);
}

// Enkel spärr mot lösenordsgissning: max 10 misslyckade försök per IP och per e-post / 15 min.
const failures = new Map<string, { count: number; until: number }>();
const WINDOW_MS = 15 * 60_000;
const MAX_FAILS = 10;

export function isBlocked(keys: string[]): boolean {
  const t = Date.now();
  return keys.some((k) => {
    const f = failures.get(k);
    return f !== undefined && f.until > t && f.count >= MAX_FAILS;
  });
}

export function registerFailure(keys: string[]): void {
  const t = Date.now();
  for (const k of keys) {
    const f = failures.get(k);
    if (!f || f.until < t) failures.set(k, { count: 1, until: t + WINDOW_MS });
    else f.count++;
  }
  if (failures.size > 10_000) {
    for (const [k, f] of failures) if (f.until < t) failures.delete(k);
  }
}

export function clearFailures(keys: string[]): void {
  for (const k of keys) failures.delete(k);
}

export function createInvite(role: Role, note: string, createdBy: number | null, days = 7): string {
  const token = randomToken(24);
  const expires = new Date(Date.now() + days * 86400_000).toISOString();
  db.prepare("INSERT INTO invites (token_hash, role, note, created_by, expires_at) VALUES (?, ?, ?, ?, ?)").run(
    sha256(token),
    role,
    note.slice(0, 200),
    createdBy,
    expires,
  );
  return token;
}

export function inviteUrl(token: string): string {
  return `${config.publicUrl}/#/inbjudan/${token}`;
}
