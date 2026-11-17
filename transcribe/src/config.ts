// Inställningar läses från miljövariabler (se .env.example). Inga hemligheter loggas.
import path from "node:path";

function env(name: string, fallback = ""): string {
  const v = process.env[name];
  return v === undefined || v.trim() === "" ? fallback : v.trim();
}

function envInt(name: string, fallback: number): number {
  const raw = env(name);
  if (!raw) return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) throw new Error(`${name} måste vara ett heltal`);
  return n;
}

const publicUrl = env("TRANSCRIBE_PUBLIC_URL", "http://localhost:8080").replace(/\/+$/, "");

export const config = {
  port: envInt("TRANSCRIBE_PORT", 8080),
  host: env("TRANSCRIBE_HOST", "0.0.0.0"),
  /** Adressen användarna når tjänsten på, t.ex. https://transcribe.smgd.se. */
  publicUrl,
  /** Säkra cookies (bara över HTTPS). Av automatiskt för http://localhost. */
  secureCookies: publicUrl.startsWith("https://"),
  dataDir: path.resolve(env("TRANSCRIBE_DATA_DIR", "./data")),
  webDir: path.resolve(env("TRANSCRIBE_WEB_DIR", "./web")),
  /** Live-utkast och talaruppdelning (Deepgram). Tomt = live-text och talare av. */
  deepgramKey: env("DEEPGRAM_API_KEY"),
  deepgramModel: env("DEEPGRAM_MODEL", "nova-3"),
  deepgramLanguage: env("DEEPGRAM_LANGUAGE", "sv"),
  deepgramUrl: env("DEEPGRAM_URL", "https://api.deepgram.com").replace(/\/+$/, ""),
  /**
   * Slutversionens text: valfri OpenAI-kompatibel Whisper-tjänst. Standard är Groqs
   * gratisnivå; byt adress för t.ex. en egen KB-Whisper-server hemma.
   */
  whisperUrl: env("WHISPER_API_URL", "https://api.groq.com/openai/v1").replace(/\/+$/, ""),
  whisperKey: env("WHISPER_API_KEY"),
  whisperModel: env("WHISPER_MODEL", "whisper-large-v3"),
  whisperLanguage: env("WHISPER_LANGUAGE", "sv"),
  /** Längsta bit (sekunder) som skickas till Whisper i ett anrop. Delas helst vid tystnad. */
  whisperChunkSeconds: envInt("WHISPER_CHUNK_SECONDS", 600),
  /** Behåll originalljudet (WAV, ~115 MB/h) efter slutversionen. Annars sparas bara M4A. */
  keepWav: env("TRANSCRIBE_KEEP_WAV", "false") === "true",
  /** Antal samtidiga inspelningar totalt (skyddar serverns minne och kvoten). */
  maxConcurrentRecordings: envInt("TRANSCRIBE_MAX_RECORDINGS", 4),
  /** Längsta tillåtna inspelning i minuter. */
  maxRecordingMinutes: envInt("TRANSCRIBE_MAX_RECORDING_MINUTES", 240),
  sessionDays: envInt("TRANSCRIBE_SESSION_DAYS", 30),
  logLevel: env("TRANSCRIBE_LOG_LEVEL", "info"),
};

export type Config = typeof config;
