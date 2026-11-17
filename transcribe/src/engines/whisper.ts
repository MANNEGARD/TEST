// Slutversionens text via en OpenAI-kompatibel Whisper-tjänst (standard: Groq, whisper-large-v3).
import fs from "node:fs";
import path from "node:path";
import { config } from "../config.js";
import type { WhisperSegment, Word } from "../merge.js";

export const whisperEnabled = (): boolean => config.whisperKey !== "" || !config.whisperUrl.includes("groq.com");

export interface WhisperResult {
  segments: WhisperSegment[];
  words: Word[];
}

export class RateLimitError extends Error {
  constructor(
    message: string,
    readonly retryAfterSec: number,
  ) {
    super(message);
  }
}

function parseRetryAfter(res: Response): number {
  const h = res.headers.get("retry-after");
  if (h) {
    const n = Number.parseFloat(h);
    if (Number.isFinite(n)) return Math.max(1, Math.ceil(n));
    const d = Date.parse(h);
    if (Number.isFinite(d)) return Math.max(1, Math.ceil((d - Date.now()) / 1000));
  }
  return 60;
}

/**
 * Transkriberar en ljudbit. `prompt` ger Whisper sammanhang: namn, facktermer och slutet av
 * föregående bit, vilket ger rätt stavning och jämnare skiljetecken över gränserna.
 */
export async function whisperFile(file: string, offset: number, prompt: string): Promise<WhisperResult> {
  const form = new FormData();
  const buf = await fs.promises.readFile(file);
  form.append("file", new Blob([new Uint8Array(buf)], { type: "audio/flac" }), path.basename(file));
  form.append("model", config.whisperModel);
  form.append("language", config.whisperLanguage);
  form.append("response_format", "verbose_json");
  form.append("temperature", "0");
  form.append("timestamp_granularities[]", "segment");
  form.append("timestamp_granularities[]", "word");
  if (prompt.trim()) form.append("prompt", prompt.trim());

  const headers: Record<string, string> = {};
  if (config.whisperKey) headers.Authorization = `Bearer ${config.whisperKey}`;
  const res = await fetch(`${config.whisperUrl}/audio/transcriptions`, {
    method: "POST",
    headers,
    body: form,
    signal: AbortSignal.timeout(15 * 60_000),
  });
  if (res.status === 429) {
    throw new RateLimitError("Whisper-tjänstens gratiskvot är tillfälligt slut.", parseRetryAfter(res));
  }
  if (!res.ok) {
    const body = (await res.text()).slice(0, 300);
    if (res.status === 401 || res.status === 403) {
      throw new Error("Whisper-tjänsten nekade nyckeln (kontrollera WHISPER_API_KEY).");
    }
    throw new Error(`Whisper-tjänsten svarade ${res.status}: ${body}`);
  }
  const json = (await res.json()) as {
    segments?: WhisperSegment[];
    words?: Array<{ word: string; start: number; end: number }>;
    text?: string;
  };
  const segments = (json.segments ?? []).map((s) => ({
    start: s.start + offset,
    end: s.end + offset,
    text: s.text,
    no_speech_prob: s.no_speech_prob,
    avg_logprob: s.avg_logprob,
    compression_ratio: s.compression_ratio,
  }));
  const words = (json.words ?? []).map((w) => ({ text: w.word, start: w.start + offset, end: w.end + offset }));
  return { segments, words };
}

/** Bygger Whispers prompt: användarens ordlista + slutet av föregående text (max ~200 tecken). */
export function buildPrompt(context: string, previousText: string): string {
  const ctx = context.replace(/\s+/g, " ").trim().slice(0, 400);
  const tail = previousText.replace(/\s+/g, " ").trim().slice(-200);
  // En kort svensk mening i början styr Whisper mot korrekt svenska med skiljetecken.
  const base = "Hej, det här är ett samtal på svenska.";
  return [ctx ? `${base} Ord som förekommer: ${ctx}.` : base, tail].filter(Boolean).join(" ").slice(-600);
}
