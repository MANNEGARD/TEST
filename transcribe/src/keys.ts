// API-nycklarna till Deepgram och Whisper-tjänsten. En nyckel som lagts in under Administration
// sparas krypterad i databasen och går före miljövariabeln (DEEPGRAM_API_KEY / WHISPER_API_KEY).
// Nyckelvärdet lämnar aldrig servern: svar och loggar innehåller högst de fyra sista tecknen.
//
// Kryptering: AES-256-GCM med en huvudnyckel på 32 slumpade byte i <datakatalogen>/secret.key.
// Lagras som iv(12) | tagg(16) | chiffertext, med nyckelns namn som extra autentiserad data så
// att ett värde inte kan flyttas till en annan rad.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { config } from "./config.js";
import { db, now } from "./db.js";

export type KeyName = "deepgram" | "whisper";
export const KEY_NAMES: readonly KeyName[] = ["deepgram", "whisper"];

export function isKeyName(v: string): v is KeyName {
  return (KEY_NAMES as readonly string[]).includes(v);
}

export interface KeyStatus {
  name: KeyName;
  /** En nyckel används just nu (från gränssnittet eller miljön). */
  set: boolean;
  source: "ui" | "env" | "none";
  /** De fyra sista tecknen i nyckeln som används; tomt om ingen eller kortare än 20 tecken. */
  hint: string;
  updatedAt: string | null;
  updatedBy: string | null;
  /** En sparad nyckel finns men går inte att dekryptera. */
  unreadable: boolean;
  envSet: boolean;
  /** false bara för en egen Whisper-server som inte kräver nyckel. */
  required: boolean;
  /** Värdnamnet nyckeln skickas till. */
  host: string;
}

export interface Check {
  result: "ok" | "denied" | "unknown" | "missing";
  detail: string;
}

/** Fel som gör att nycklar inte kan sparas (t.ex. en skadad secret.key). */
export class KeyStoreError extends Error {}

const IV_BYTES = 12;
const TAG_BYTES = 16;
const MASTER_BYTES = 32;

function aad(name: string): Buffer {
  return Buffer.from(`transcribe:service_keys:${name}`, "utf8");
}

export function encryptValue(master: Buffer, name: string, value: string): Buffer {
  const iv = crypto.randomBytes(IV_BYTES);
  const cipher = crypto.createCipheriv("aes-256-gcm", master, iv);
  cipher.setAAD(aad(name));
  const ct = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), ct]);
}

/** Kastar ett fel om värdet inte går att dekryptera (fel huvudnyckel, fel rad, ändrade byte). */
export function decryptValue(master: Buffer, name: string, blob: Buffer): string {
  if (blob.length < IV_BYTES + TAG_BYTES + 1) throw new Error("För kort chiffertext");
  const decipher = crypto.createDecipheriv("aes-256-gcm", master, blob.subarray(0, IV_BYTES));
  decipher.setAAD(aad(name));
  decipher.setAuthTag(blob.subarray(IV_BYTES, IV_BYTES + TAG_BYTES));
  return Buffer.concat([decipher.update(blob.subarray(IV_BYTES + TAG_BYTES)), decipher.final()]).toString("utf8");
}

export function secretKeyFile(): string {
  return path.join(config.dataDir, "secret.key");
}

let master: Buffer | null = null;
/** Dekrypterade värden, knutna till chiffertexten de kom från. */
const plain = new Map<KeyName, { blob: Buffer; value: string | null }>();

/** Glömmer huvudnyckeln och dekrypterade värden (används av proven). */
export function clearKeyCache(): void {
  master = null;
  plain.clear();
}

/**
 * Läser huvudnyckeln. Med `create` skapas filen (0600, exklusivt) om den saknas; annars ges
 * null. En fil med fel längd skrivs aldrig över utan ger KeyStoreError.
 */
function masterKey(create: boolean): Buffer | null {
  if (master) return master;
  const file = secretKeyFile();
  let buf: Buffer;
  try {
    buf = fs.readFileSync(file);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new KeyStoreError("Filen secret.key i datakatalogen går inte att läsa. Nycklar kan inte sparas.");
    }
    if (!create) return null;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const fresh = crypto.randomBytes(MASTER_BYTES);
    let fd: number | null = null;
    try {
      fd = fs.openSync(file, "wx", 0o600);
      fs.fchmodSync(fd, 0o600);
      fs.writeSync(fd, fresh);
      fs.fsyncSync(fd);
      buf = fresh;
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
      // En annan process hann skapa filen: använd den.
      buf = fs.readFileSync(file);
    } finally {
      if (fd !== null) fs.closeSync(fd);
    }
  }
  if (buf.length !== MASTER_BYTES) {
    throw new KeyStoreError(
      "Filen secret.key i datakatalogen är skadad (fel längd). Nycklar kan inte sparas förrän den är åtgärdad.",
    );
  }
  master = buf;
  return master;
}

/** Kontrollerar att nycklar kan sparas (huvudnyckeln finns eller kan skapas). */
export function ensureWritable(): void {
  masterKey(true);
}

interface StoredRow {
  ciphertext: Buffer;
  updated_at: string;
  email: string | null;
}

function storedRow(name: KeyName): StoredRow | undefined {
  return db
    .prepare(
      `SELECT k.ciphertext, k.updated_at, u.email FROM service_keys k
       LEFT JOIN users u ON u.id = k.updated_by WHERE k.name = ?`,
    )
    .get(name) as StoredRow | undefined;
}

/** Den sparade nyckeln i klartext, eller null om den inte går att dekryptera. */
function storedValue(name: KeyName, row: StoredRow): string | null {
  const cached = plain.get(name);
  if (cached && cached.blob.equals(row.ciphertext)) return cached.value;
  let value: string | null = null;
  try {
    const m = masterKey(false);
    if (m) value = decryptValue(m, name, row.ciphertext);
  } catch {
    value = null;
  }
  plain.set(name, { blob: Buffer.from(row.ciphertext), value });
  return value;
}

function envValue(name: KeyName): string {
  return name === "deepgram" ? config.deepgramKey : config.whisperKey;
}

function serviceUrl(name: KeyName): string {
  return name === "deepgram" ? config.deepgramUrl : config.whisperUrl;
}

/** Tjänstens namn i meddelanden till användaren. */
export function serviceLabel(name: KeyName): string {
  if (name === "deepgram") return "Deepgram";
  return config.whisperUrl.includes("groq.com") ? "Groq" : "Whisper-tjänsten";
}

/** Nyckeln som ska användas just nu: gränssnittets om den går att läsa, annars miljöns, annars "". */
export function getKey(name: KeyName): string {
  const row = storedRow(name);
  const value = row ? storedValue(name, row) : null;
  return value || envValue(name);
}

export function keyRequired(name: KeyName): boolean {
  return name === "deepgram" || config.whisperUrl.includes("groq.com");
}

export function status(name: KeyName): KeyStatus {
  const row = storedRow(name);
  const value = row ? storedValue(name, row) : null;
  const env = envValue(name);
  const source = value ? "ui" : env ? "env" : "none";
  const used = value || env;
  return {
    name,
    set: used !== "",
    source,
    hint: used.length >= 20 ? used.slice(-4) : "",
    updatedAt: source === "ui" ? row!.updated_at : null,
    updatedBy: source === "ui" ? row!.email : null,
    unreadable: row !== undefined && value === null,
    envSet: env !== "",
    required: keyRequired(name),
    host: new URL(serviceUrl(name)).host,
  };
}

export function setKey(name: KeyName, value: string, userId: number | null): void {
  const m = masterKey(true)!;
  const blob = encryptValue(m, name, value);
  db.prepare(
    `INSERT INTO service_keys (name, ciphertext, updated_at, updated_by) VALUES (?, ?, ?, ?)
     ON CONFLICT (name) DO UPDATE SET ciphertext = excluded.ciphertext, updated_at = excluded.updated_at,
       updated_by = excluded.updated_by`,
  ).run(name, blob, now(), userId);
  plain.delete(name);
}

export function deleteKey(name: KeyName): void {
  db.prepare("DELETE FROM service_keys WHERE name = ?").run(name);
  plain.delete(name);
}

/** Granskar en inklistrad nyckel. Ger det rensade värdet eller ett fel att visa användaren. */
export function validateKey(raw: unknown): { value: string } | { error: string } {
  if (typeof raw !== "string") return { error: "Nyckeln saknas." };
  const value = raw.trim();
  if (value === "") return { error: "Klistra in nyckeln först." };
  if (value.length < 8) return { error: "Nyckeln är för kort. Kontrollera att hela nyckeln kom med." };
  if (value.length > 512) return { error: "Nyckeln är för lång (högst 512 tecken)." };
  if (!/^[\x21-\x7e]+$/.test(value)) {
    return { error: "Nyckeln innehåller tecken som inte hör hemma i en nyckel, till exempel mellanslag eller radbrytningar." };
  }
  return { value };
}

const CHECK_TIMEOUT_MS = 10_000;

/**
 * Frågar tjänsten om nyckeln godtas. Bygger bara på statuskoden: 2xx godkänd, 401 nekad,
 * allt annat okänt. Svarstexten används aldrig och omdirigeringar följs inte.
 */
export async function checkKey(name: KeyName, key: string): Promise<Check> {
  const label = serviceLabel(name);
  const url = name === "deepgram" ? `${config.deepgramUrl}/v1/projects` : `${config.whisperUrl}/models`;
  const authorization = name === "deepgram" ? `Token ${key}` : `Bearer ${key}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: "GET",
      headers: { Authorization: authorization },
      redirect: "manual",
      signal: AbortSignal.timeout(CHECK_TIMEOUT_MS),
    });
  } catch (e) {
    const timeout = e instanceof Error && (e.name === "TimeoutError" || e.name === "AbortError");
    return {
      result: "unknown",
      detail: timeout ? `${label} svarade inte inom ${CHECK_TIMEOUT_MS / 1000} sekunder.` : `${label} gick inte att nå.`,
    };
  }
  await res.body?.cancel().catch(() => undefined);
  if (res.status >= 200 && res.status < 300) return { result: "ok", detail: "Nyckeln fungerar." };
  if (res.status === 401) return { result: "denied", detail: `${label} godkände inte nyckeln.` };
  return { result: "unknown", detail: `${label} svarade ${res.status}.` };
}

/** Kontrollerar en given nyckel, eller den som används just nu. */
export async function testKey(name: KeyName, value?: string): Promise<Check> {
  const key = value ?? getKey(name);
  if (!key) return { result: "missing", detail: "Ingen nyckel är inlagd." };
  return checkKey(name, key);
}
