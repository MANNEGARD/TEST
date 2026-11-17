// Deepgram: live-transkribering över WebSocket och talaruppdelning av hela inspelningen.
import fs from "node:fs";
import WebSocket from "ws";
import { config } from "../config.js";
import type { Word } from "../merge.js";

export const deepgramEnabled = (): boolean => config.deepgramKey !== "";

interface DgWord {
  word: string;
  punctuated_word?: string;
  start: number;
  end: number;
  speaker?: number;
}

interface DgResults {
  type: "Results";
  is_final?: boolean;
  speech_final?: boolean;
  channel?: { alternatives?: Array<{ transcript?: string; words?: DgWord[] }> };
}

function toWords(ws: DgWord[] | undefined, offset = 0): Word[] {
  return (ws ?? []).map((w) => ({
    text: w.punctuated_word ?? w.word,
    start: w.start + offset,
    end: w.end + offset,
    speaker: w.speaker === undefined ? undefined : String(w.speaker),
  }));
}

export interface LiveCallbacks {
  onFinal(words: Word[]): void;
  onInterim(words: Word[]): void;
  onError(message: string): void;
  onClose(): void;
}

/** En live-anslutning. Tidsstämplar förskjuts med `offset` (fortsättning efter avbrott). */
export class DeepgramLive {
  private ws: WebSocket;
  private queue: Buffer[] = [];
  private open = false;
  private closed = false;
  private keepAlive: NodeJS.Timeout;
  private lastSend = Date.now();

  constructor(
    private offset: number,
    private cb: LiveCallbacks,
  ) {
    const params = new URLSearchParams({
      model: config.deepgramModel,
      language: config.deepgramLanguage,
      encoding: "linear16",
      sample_rate: "16000",
      channels: "1",
      diarize: "true",
      punctuate: "true",
      smart_format: "true",
      interim_results: "true",
      endpointing: "400",
      utterance_end_ms: "1500",
      vad_events: "true",
    });
    const url = `${config.deepgramUrl.replace(/^http/, "ws")}/v1/listen?${params}`;
    this.ws = new WebSocket(url, { headers: { Authorization: `Token ${config.deepgramKey}` } });
    this.ws.on("open", () => {
      this.open = true;
      for (const b of this.queue) this.ws.send(b);
      this.queue = [];
    });
    this.ws.on("message", (data, isBinary) => {
      if (isBinary) return;
      let msg: DgResults | { type: string; description?: string };
      try {
        msg = JSON.parse(data.toString());
      } catch {
        return;
      }
      if (msg.type === "Results") {
        const r = msg as DgResults;
        const words = toWords(r.channel?.alternatives?.[0]?.words, this.offset);
        if (r.is_final) {
          if (words.length) this.cb.onFinal(words);
        } else {
          this.cb.onInterim(words);
        }
      } else if (msg.type === "Error") {
        this.cb.onError(String((msg as { description?: string }).description ?? "Okänt fel från Deepgram"));
      }
    });
    this.ws.on("unexpected-response", (_req, res) => {
      this.cb.onError(
        res.statusCode === 401 || res.statusCode === 403
          ? "Deepgram nekade nyckeln (kontrollera DEEPGRAM_API_KEY)."
          : res.statusCode === 402
            ? "Deepgram-krediten är slut."
            : `Deepgram svarade ${res.statusCode}.`,
      );
    });
    this.ws.on("error", (e) => {
      if (!this.closed) this.cb.onError(`Ingen kontakt med Deepgram: ${e.message}`);
    });
    this.ws.on("close", () => {
      this.closed = true;
      clearInterval(this.keepAlive);
      this.cb.onClose();
    });
    // Deepgram stänger efter ~10 s utan ljud; skicka KeepAlive när ingenting skickas (paus).
    this.keepAlive = setInterval(() => {
      if (this.open && !this.closed && Date.now() - this.lastSend > 5000) {
        this.ws.send(JSON.stringify({ type: "KeepAlive" }));
      }
    }, 4000);
  }

  send(pcm: Buffer): void {
    if (this.closed) return;
    this.lastSend = Date.now();
    if (this.open) this.ws.send(pcm);
    else if (this.queue.length < 600) this.queue.push(pcm); // max ~1 min i väntan på anslutning
  }

  /** Ber Deepgram skicka sista resultaten och stänga. Löser när anslutningen stängts. */
  finish(timeoutMs = 8000): Promise<void> {
    return new Promise((resolve) => {
      if (this.closed) return resolve();
      const done = () => resolve();
      this.ws.once("close", done);
      try {
        if (this.open) this.ws.send(JSON.stringify({ type: "CloseStream" }));
        else this.ws.close();
      } catch {
        /* redan stängd */
      }
      setTimeout(() => {
        try {
          this.ws.terminate();
        } catch {
          /* ignorera */
        }
        resolve();
      }, timeoutMs);
    });
  }

  abort(): void {
    this.closed = true;
    clearInterval(this.keepAlive);
    try {
      this.ws.terminate();
    } catch {
      /* ignorera */
    }
  }
}

export class QuotaError extends Error {}

/** Hela inspelningen i ett anrop: ord med talare (bästa talaruppdelningen). */
export async function deepgramFile(file: string, contentType: string, keyterms: string[] = []): Promise<Word[]> {
  const params = new URLSearchParams({
    model: config.deepgramModel,
    language: config.deepgramLanguage,
    diarize: "true",
    punctuate: "true",
    smart_format: "true",
    utterances: "true",
  });
  for (const k of keyterms.slice(0, 50)) params.append("keyterm", k);
  const size = fs.statSync(file).size;
  const res = await fetch(`${config.deepgramUrl}/v1/listen?${params}`, {
    method: "POST",
    headers: {
      Authorization: `Token ${config.deepgramKey}`,
      "Content-Type": contentType,
      "Content-Length": String(size),
    },
    body: fs.createReadStream(file) as unknown as BodyInit,
    duplex: "half",
    signal: AbortSignal.timeout(30 * 60_000),
  } as RequestInit);
  if (!res.ok) {
    const body = (await res.text()).slice(0, 300);
    if (res.status === 402) throw new QuotaError("Deepgram-krediten är slut.");
    // Okända söktermer (keyterm) för språket ska inte stoppa jobbet – försök igen utan.
    if (res.status === 400 && keyterms.length) return deepgramFile(file, contentType, []);
    throw new Error(`Deepgram svarade ${res.status}: ${body}`);
  }
  const json = (await res.json()) as {
    results?: { channels?: Array<{ alternatives?: Array<{ words?: DgWord[] }> }> };
  };
  return toWords(json.results?.channels?.[0]?.alternatives?.[0]?.words);
}
