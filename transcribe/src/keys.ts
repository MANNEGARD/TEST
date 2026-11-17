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

/** En nyckel som behövs saknas (t.ex. borttagen under Administration mitt i ett jobb). */
export class MissingKeyError extends Error {}

type Warn = (obj: Record<string, unknown>, msg: string) => void;
let warn: Warn = (obj, msg) => console.warn(msg, obj);

/** Låter servern ta emot varningar från nyckellagringen i sin vanliga logg. */
export function setKeyLogger(logger: { warn: Warn }): void {
  warn = (obj, msg) => logger.warn(obj, msg);
}

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
 * Läser en befintlig huvudnyckelfil. Kan andra konton läsa den snävas rättigheterna in till 0600
 * (innehållet skrivs aldrig om) och en varning loggas. Kastar ENOENT om filen saknas.
 */
function readMasterFile(file: string): Buffer {
  const fd = fs.openSync(file, "r");
  try {
    const mode = fs.fstatSync(fd).mode & 0o777;
    if (mode & 0o077) {
      let fixed = true;
      try {
        fs.fchmodSync(fd, 0o600);
      } catch {
        fixed = false;
      }
      warn(
        { file, mode: mode.toString(8) },
        fixed
          ? "Filen secret.key kunde läsas av andra konton på servern. Rättigheterna har ändrats till 0600."
          : "Filen secret.key kan läsas av andra konton på servern. Ändra rättigheterna till 0600.",
      );
    }
    return fs.readFileSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

const NOT_WRITABLE = "Datakatalogen går inte att skriva till. Nyckeln kunde inte sparas.";

/**
 * Skapar huvudnyckelfilen så att den antingen finns hel eller inte alls: de 32 bytena skrivs
 * först till en tillfällig fil (0600) som sedan länkas in under det riktiga namnet. Länkningen
 * misslyckas om filen redan finns (då används den), så en befintlig fil skrivs aldrig över.
 */
function createMasterFile(file: string): Buffer {
  const fresh = crypto.randomBytes(MASTER_BYTES);
  const tmp = `${file}.tmp-${crypto.randomBytes(6).toString("hex")}`;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const fd = fs.openSync(tmp, "wx", 0o600);
    try {
      fs.fchmodSync(fd, 0o600);
      if (fs.writeSync(fd, fresh) !== MASTER_BYTES) throw new Error("Ofullständig skrivning");
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    try {
      fs.linkSync(tmp, file);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === "EEXIST") return readMasterFile(file); // en annan process hann före
      if (code !== "EPERM" && code !== "ENOTSUP" && code !== "EOPNOTSUPP") throw err;
      // Filsystemet saknar hårda länkar: skapa filen exklusivt och skriv direkt i den.
      let direct: number;
      try {
        direct = fs.openSync(file, "wx", 0o600);
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code === "EEXIST") return readMasterFile(file);
        throw e;
      }
      try {
        fs.writeSync(direct, fresh);
        fs.fsyncSync(direct);
      } finally {
        fs.closeSync(direct);
      }
    }
    return fresh;
  } catch (err) {
    if (err instanceof KeyStoreError) throw err;
    throw new KeyStoreError(NOT_WRITABLE);
  } finally {
    try {
      fs.rmSync(tmp, { force: true });
    } catch {
      /* den tillfälliga filen är redan borta eller går inte att ta bort */
    }
  }
}

/**
 * Läser eller (med `create`) skapar huvudnyckeln i `file`. Utan `create` ges null om filen
 * saknas. En fil med fel längd skrivs aldrig över utan ger KeyStoreError.
 */
export function loadMasterKey(file: string, create: boolean): Buffer | null {
  let buf: Buffer;
  try {
    buf = readMasterFile(file);
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    // ENOTDIR: en del av sökvägen är ingen katalog, så filen finns inte.
    if (code !== "ENOENT" && code !== "ENOTDIR") {
      throw new KeyStoreError("Filen secret.key i datakatalogen går inte att läsa. Nycklar kan inte sparas.");
    }
    if (!create) return null;
    buf = createMasterFile(file);
  }
  if (buf.length !== MASTER_BYTES) {
    throw new KeyStoreError(
      "Filen secret.key i datakatalogen är skadad (fel längd). Nycklar kan inte sparas förrän den är åtgärdad.",
    );
  }
  return buf;
}

function masterKey(create: boolean): Buffer | null {
  if (master) return master;
  master = loadMasterKey(secretKeyFile(), create);
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
