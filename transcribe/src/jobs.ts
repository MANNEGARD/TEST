// Bearbetningskö för slutversionen. Körs i samma process, ett jobb i taget (skonsamt mot en
// liten VPS och mot gratiskvoterna). Jobb som var på gång vid en omstart tas upp igen.
import fs from "node:fs";
import path from "node:path";
import type { FastifyBaseLogger } from "fastify";
import {
  BYTES_PER_SECOND,
  detectSilences,
  extractFlac,
  planChunks,
  probeDuration,
  repairWav,
  toM4a,
  toWav,
} from "./audio.js";
import { config } from "./config.js";
import { addUsage, audioDir, db, now, parseJson, reindex, type Segment, type TranscriptRow } from "./db.js";
import { deepgramEnabled, deepgramFile } from "./engines/deepgram.js";
import { buildPrompt, RateLimitError, whisperEnabled, whisperFile } from "./engines/whisper.js";
import {
  assignSpeakers,
  cleanWhisper,
  normalizeSpeakers,
  SpeakerTimeline,
  wordsToSegments,
  type WhisperSegment,
  type Word,
} from "./merge.js";

type Log = Pick<FastifyBaseLogger, "info" | "warn" | "error">;
let log: Log = {
  info: (...a: unknown[]) => console.log(...a),
  warn: (...a: unknown[]) => console.warn(...a),
  error: (...a: unknown[]) => console.error(...a),
} as unknown as Log;
const queue: string[] = [];
let running = false;

export function initJobs(logger: FastifyBaseLogger): void {
  log = logger;
  const pending = db.prepare("SELECT id FROM transcripts WHERE status = 'processing' ORDER BY created_at").all() as Array<{
    id: string;
  }>;
  for (const p of pending) enqueue(p.id);
}

export function enqueue(id: string): void {
  if (!queue.includes(id)) queue.push(id);
  void pump();
}

export function queuePosition(id: string): number {
  return queue.indexOf(id);
}

async function pump(): Promise<void> {
  if (running) return;
  running = true;
  try {
    while (queue.length) {
      const id = queue[0]!;
      try {
        await processTranscript(id);
      } catch (e) {
        log.error({ err: e, id }, "Bearbetningen misslyckades");
        setStatus(id, { status: "failed", status_detail: "", error: e instanceof Error ? e.message : String(e) });
      }
      queue.shift();
    }
  } finally {
    running = false;
  }
}

function setStatus(id: string, fields: Partial<Pick<TranscriptRow, "status" | "status_detail" | "error">>): void {
  const sets = Object.keys(fields).map((k) => `${k} = @${k}`);
  db.prepare(`UPDATE transcripts SET ${sets.join(", ")}, updated_at = @now WHERE id = @id`).run({ ...fields, id, now: now() });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function exists(file: string): boolean {
  try {
    return fs.statSync(file).size > 0;
  } catch {
    return false;
  }
}

async function processTranscript(id: string): Promise<void> {
  const row = db.prepare("SELECT * FROM transcripts WHERE id = ?").get(id) as TranscriptRow | undefined;
  if (!row || row.status !== "processing") return;
  const dir = audioDir(id);
  const wav = path.join(dir, "recording.wav");
  const m4a = path.join(dir, "audio.m4a");
  const tmpDir = path.join(dir, "tmp");
  fs.rmSync(tmpDir, { recursive: true, force: true });
  fs.mkdirSync(tmpDir, { recursive: true });

  try {
    // 1. Ljudet: WAV från inspelningen (eller återskapad från M4A vid omkörning).
    setStatus(id, { status_detail: "Förbereder ljudet", error: "" });
    let source = wav;
    let duration: number;
    if (exists(wav)) {
      duration = repairWav(wav) / BYTES_PER_SECOND;
      if (duration < 0.5) throw new Error("Inspelningen innehåller inget ljud.");
      await toM4a(wav, m4a);
    } else if (exists(m4a)) {
      source = path.join(tmpDir, "source.wav");
      await toWav(m4a, source);
      duration = await probeDuration(m4a);
    } else {
      throw new Error("Ljudfilen saknas.");
    }
    db.prepare("UPDATE transcripts SET has_audio = 1, duration_sec = ? WHERE id = ?").run(duration, id);

    const keyterms = row.context
      .split(/[,;\n]/)
      .map((s) => s.trim())
      .filter((s) => s.length > 1 && s.length < 60);
    const notes: string[] = [];

    // 2. Talaruppdelning av hela inspelningen.
    let dgWords: Word[] = [];
    if (deepgramEnabled()) {
      setStatus(id, { status_detail: "Delar upp talare" });
      try {
        dgWords = await deepgramFile(m4a, "audio/mp4", keyterms);
        addUsage(row.user_id, "deepgram-fil", duration);
      } catch (e) {
        log.warn({ err: e, id }, "Talaruppdelningen misslyckades");
        notes.push(`Talaruppdelningen misslyckades: ${e instanceof Error ? e.message : e}`);
      }
    }

    // 3. Exakt text med Whisper, i bitar som delas vid tystnad.
    let wSegments: WhisperSegment[] | null = null;
    let wWords: Word[] = [];
    if (whisperEnabled()) {
      try {
        const silences = await detectSilences(source);
        const chunks = planChunks(duration, silences, config.whisperChunkSeconds);
        wSegments = [];
        let previous = "";
        for (let i = 0; i < chunks.length; i++) {
          const c = chunks[i]!;
          const part = chunks.length > 1 ? ` (del ${i + 1} av ${chunks.length})` : "";
          setStatus(id, { status_detail: `Transkriberar${part}` });
          const flac = path.join(tmpDir, `chunk-${i}.flac`);
          await extractFlac(source, flac, c.start, c.end - c.start);
          const result = await withRetry(id, part, () => whisperFile(flac, c.start, buildPrompt(row.context, previous)));
          fs.rmSync(flac, { force: true });
          wSegments.push(...result.segments);
          wWords.push(...result.words);
          previous = result.segments.map((s) => s.text).join(" ");
        }
        addUsage(row.user_id, "whisper", duration);
      } catch (e) {
        log.warn({ err: e, id }, "Whisper misslyckades");
        notes.push(`Whisper misslyckades: ${e instanceof Error ? e.message : e}`);
        wSegments = null;
        wWords = [];
      }
    }

    // 4. Slå ihop.
    let segments: Segment[];
    let engine: string;
    if (wSegments) {
      const timeline = new SpeakerTimeline(dgWords);
      segments = assignSpeakers(cleanWhisper(wSegments, timeline), wWords, timeline);
      engine = dgWords.length ? "whisper+deepgram" : "whisper";
    } else if (dgWords.length) {
      segments = wordsToSegments(dgWords);
      engine = "deepgram";
    } else {
      const reason = notes.length
        ? notes.join(" ")
        : "Ingen transkriberingstjänst är inställd. Lägg in nycklarna under Administration.";
      throw new Error(reason);
    }
    segments = normalizeSpeakers(segments);

    db.prepare(
      `UPDATE transcripts SET segments = ?, final_engine = ?, status = 'ready', status_detail = '',
       error = ?, updated_at = ? WHERE id = ?`,
    ).run(JSON.stringify(segments), engine, notes.join(" "), now(), id);
    reindex(id);

    if (!config.keepWav && exists(m4a) && source === wav) fs.rmSync(wav, { force: true });
    log.info({ id, engine, duration: Math.round(duration), segments: segments.length }, "Slutversion klar");
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

/** Väntar ut kvotgränser (Groqs gratisnivå har tim- och dygnsgränser) och tillfälliga fel. */
async function withRetry<T>(id: string, part: string, fn: () => Promise<T>): Promise<T> {
  const deadline = Date.now() + 26 * 3600_000;
  let attempt = 0;
  for (;;) {
    try {
      return await fn();
    } catch (e) {
      if (e instanceof RateLimitError && Date.now() < deadline) {
        const wait = Math.min(e.retryAfterSec, 3600);
        const until = new Date(Date.now() + wait * 1000).toLocaleTimeString("sv-SE", {
          timeZone: "Europe/Stockholm",
          hour: "2-digit",
          minute: "2-digit",
        });
        setStatus(id, { status_detail: `Väntar på gratiskvoten${part} – fortsätter ca ${until}` });
        await sleep(wait * 1000 + 1000);
        continue;
      }
      attempt++;
      if (attempt >= 4 || (e instanceof Error && /nekade nyckeln/.test(e.message))) throw e;
      await sleep(5000 * 2 ** attempt);
    }
  }
}

/** Live-segmenten som reserv när ingenting annat finns (visas som utkast). */
export function liveSegments(row: TranscriptRow): Segment[] {
  return parseJson<Segment[]>(row.live_segments, []);
}
