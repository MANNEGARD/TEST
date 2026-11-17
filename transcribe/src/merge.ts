// Ren logik (inga sidoeffekter) för att bygga stycken av ord, rensa bort Whispers kända
// "hallucinationer" och koppla Whispers text till Deepgrams talaruppdelning. Testas i test/.
import type { Segment } from "./db.js";

export interface Word {
  text: string;
  start: number;
  end: number;
  speaker?: string;
}

export interface WhisperSegment {
  start: number;
  end: number;
  text: string;
  no_speech_prob?: number;
  avg_logprob?: number;
  compression_ratio?: number;
}

/** Ny paragraf vid talarbyte, vid paus längre än detta, eller när stycket blivit långt. */
const PAUSE_BREAK = 2.0;
const MAX_SEGMENT = 60;

function endsSentence(text: string): boolean {
  return /[.!?…]["”']?$/.test(text.trim());
}

/** Grupperar ord (med talare) till stycken. */
export function wordsToSegments(words: Word[]): Segment[] {
  const out: Segment[] = [];
  let cur: Segment | null = null;
  for (const w of words) {
    const speaker = w.speaker ?? "0";
    const text = w.text.trim();
    if (!text) continue;
    const breakHere =
      cur !== null &&
      (cur.speaker !== speaker ||
        w.start - cur.end > PAUSE_BREAK ||
        (cur.end - cur.start > MAX_SEGMENT && endsSentence(cur.text)));
    if (cur === null || breakHere) {
      if (cur) out.push(cur);
      cur = { speaker, start: w.start, end: w.end, text };
    } else {
      cur.text += ` ${text}`;
      cur.end = Math.max(cur.end, w.end);
    }
  }
  if (cur) out.push(cur);
  return out;
}

/** Slår ihop intilliggande stycken med samma talare (för läsbarhet). */
export function joinSegments(segs: Segment[]): Segment[] {
  const out: Segment[] = [];
  for (const s of segs) {
    const text = s.text.trim();
    if (!text) continue;
    const prev = out[out.length - 1];
    if (
      prev &&
      prev.speaker === s.speaker &&
      s.start - prev.end <= PAUSE_BREAK &&
      !(prev.end - prev.start > MAX_SEGMENT && endsSentence(prev.text))
    ) {
      prev.text = `${prev.text} ${text}`;
      prev.end = Math.max(prev.end, s.end);
    } else {
      out.push({ ...s, text });
    }
  }
  return out;
}

// Fraser som Whisper (tränad på undertexter) hittar på i tystnad eller brus.
const HALLUCINATIONS = [
  /textning\.nu/i,
  /svensktextning/i,
  /undertext(er|ning)? (från|av) /i,
  /amara\.org/i,
  /^(tack för att (ni|du) (har )?(tittat|tittade|lyssnat|lyssnade)[.!]*\s*)+$/i,
  /prenumerera på (kanalen|min kanal)/i,
  /^(\s*musik\s*|\s*\[.*?\]\s*|\s*\(.*?\)\s*)$/i,
  /^[\s.…,!?-]*$/,
];

export function isHallucination(seg: WhisperSegment): boolean {
  const text = seg.text.trim();
  if (HALLUCINATIONS.some((re) => re.test(text))) return true;
  if ((seg.compression_ratio ?? 0) > 2.6) return true; // upprepningsslingor
  if ((seg.no_speech_prob ?? 0) > 0.6 && (seg.avg_logprob ?? 0) < -1.0) return true;
  return false;
}

/** Tar bort upprepade identiska meningar som Whisper ibland fastnar i. */
export function dropRepeats(segs: WhisperSegment[]): WhisperSegment[] {
  const out: WhisperSegment[] = [];
  let repeat = 0;
  for (const s of segs) {
    const prev = out[out.length - 1];
    if (prev && prev.text.trim().toLowerCase() === s.text.trim().toLowerCase()) {
      repeat++;
      if (repeat >= 2) continue;
    } else {
      repeat = 0;
    }
    out.push(s);
  }
  return out;
}

/** Talartidslinje från Deepgrams ord (sorterad), med uppslag för en tidpunkt. */
export class SpeakerTimeline {
  private words: Word[];

  constructor(words: Word[]) {
    this.words = words.filter((w) => w.speaker !== undefined).sort((a, b) => a.start - b.start);
  }

  get empty(): boolean {
    return this.words.length === 0;
  }

  /** Talare vid tiden t: ordet som täcker t, annars närmaste ord inom maxGap sekunder. */
  speakerAt(t: number, maxGap = 1.5): string | undefined {
    const ws = this.words;
    let lo = 0;
    let hi = ws.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (ws[mid]!.start <= t) lo = mid + 1;
      else hi = mid - 1;
    }
    // hi = sista ordet som börjar före t
    const cands = [ws[hi], ws[hi + 1]].filter((w): w is Word => w !== undefined);
    let best: Word | undefined;
    let bestDist = Infinity;
    for (const w of cands) {
      const dist = t < w.start ? w.start - t : t > w.end ? t - w.end : 0;
      if (dist < bestDist) {
        bestDist = dist;
        best = w;
      }
    }
    return best && bestDist <= maxGap ? best.speaker : undefined;
  }

  /** Talare med mest taltid inom [start, end]. */
  dominant(start: number, end: number): string | undefined {
    const totals = new Map<string, number>();
    for (const w of this.words) {
      if (w.end < start) continue;
      if (w.start > end) break;
      const overlap = Math.min(end, w.end) - Math.max(start, w.start);
      if (overlap > 0) totals.set(w.speaker!, (totals.get(w.speaker!) ?? 0) + overlap);
    }
    let best: string | undefined;
    let max = 0;
    for (const [s, v] of totals) {
      if (v > max) {
        max = v;
        best = s;
      }
    }
    return best;
  }

  /** Antal ord Deepgram hörde inom intervallet (för att avslöja påhittad text). */
  countWords(start: number, end: number): number {
    let n = 0;
    for (const w of this.words) {
      if (w.end < start) continue;
      if (w.start > end) break;
      n++;
    }
    return n;
  }
}

/** Ersätter talarbyten som bara gäller ett enstaka ord (oftast fel i uppdelningen). */
function smooth(speakers: string[]): string[] {
  const s = [...speakers];
  for (let i = 1; i < s.length - 1; i++) {
    if (s[i] !== s[i - 1] && s[i - 1] === s[i + 1]) s[i] = s[i - 1]!;
  }
  return s;
}

/**
 * Kopplar Whispers stycken (bäst text och skiljetecken) till Deepgrams talare (bäst
 * talaruppdelning). Ett Whisper-stycke som spänner över ett talarbyte delas vid rätt ord
 * när orden går att para ihop med texten, annars får det den talare som pratar mest.
 */
export function assignSpeakers(segments: WhisperSegment[], words: Word[], timeline: SpeakerTimeline): Segment[] {
  const out: Segment[] = [];
  let lastSpeaker = "0";
  let wi = 0;
  const sortedWords = [...words].sort((a, b) => a.start - b.start);
  for (const seg of segments) {
    const text = seg.text.trim();
    if (!text) continue;
    // Ord vars mittpunkt ligger inom stycket.
    while (wi < sortedWords.length && (sortedWords[wi]!.start + sortedWords[wi]!.end) / 2 < seg.start - 0.05) wi++;
    const segWords: Word[] = [];
    for (let j = wi; j < sortedWords.length; j++) {
      const w = sortedWords[j]!;
      if ((w.start + w.end) / 2 > seg.end + 0.05) break;
      segWords.push(w);
    }
    const tokens = text.split(/\s+/);
    if (!timeline.empty && segWords.length > 0 && tokens.length === segWords.length) {
      const raw = segWords.map((w) => timeline.speakerAt((w.start + w.end) / 2) ?? lastSpeaker);
      const speakers = smooth(raw);
      let run: Segment | null = null;
      for (let k = 0; k < tokens.length; k++) {
        const sp = speakers[k]!;
        const w = segWords[k]!;
        if (run && run.speaker === sp) {
          run.text += ` ${tokens[k]}`;
          run.end = w.end;
        } else {
          if (run) out.push(run);
          run = { speaker: sp, start: k === 0 ? seg.start : w.start, end: w.end, text: tokens[k]! };
        }
      }
      if (run) {
        run.end = Math.max(run.end, seg.end);
        out.push(run);
        lastSpeaker = run.speaker;
      }
    } else {
      const sp = (timeline.empty ? undefined : timeline.dominant(seg.start, seg.end)) ?? lastSpeaker;
      out.push({ speaker: sp, start: seg.start, end: seg.end, text });
      lastSpeaker = sp;
    }
  }
  return joinSegments(out);
}

/** Rensar Whisper-stycken: kända påhitt, upprepningar och text där Deepgram inte hörde tal. */
export function cleanWhisper(segments: WhisperSegment[], timeline: SpeakerTimeline): WhisperSegment[] {
  const kept = segments.filter((s) => {
    if (isHallucination(s)) return false;
    if (!timeline.empty && timeline.countWords(s.start - 0.3, s.end + 0.3) === 0) {
      // Deepgram hörde inget alls här. Kort text med viss osäkerhet är nästan alltid påhittad.
      const words = s.text.trim().split(/\s+/).length;
      if (words <= 8 || (s.no_speech_prob ?? 0) > 0.3) return false;
    }
    return true;
  });
  return dropRepeats(kept);
}

/** Byter talar-id mot 0, 1, 2 … i den ordning de först hörs. */
export function normalizeSpeakers(segs: Segment[]): Segment[] {
  const map = new Map<string, string>();
  return segs.map((s) => {
    if (!map.has(s.speaker)) map.set(s.speaker, String(map.size));
    return { ...s, speaker: map.get(s.speaker)! };
  });
}

export function formatTime(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const mm = String(m).padStart(2, "0");
  const sss = String(ss).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${sss}` : `${mm}:${sss}`;
}

export function speakerName(speakers: Record<string, string>, id: string): string {
  const n = speakers[id]?.trim();
  return n ? n : `Talare ${Number(id) + 1}`;
}

export interface TxtOptions {
  timestamps: boolean;
  speakers: boolean;
}

/** Textfil för export: rubrik, metadata och ett stycke per talarinlägg. */
export function toTxt(
  meta: { title: string; createdAt: string; durationSec: number; draft: boolean },
  segs: Segment[],
  speakers: Record<string, string>,
  opts: TxtOptions,
): string {
  const date = new Date(meta.createdAt);
  const dateStr = date.toLocaleString("sv-SE", { timeZone: "Europe/Stockholm", dateStyle: "long", timeStyle: "short" });
  const lines: string[] = [meta.title, "=".repeat(Math.min(meta.title.length, 80) || 3), ""];
  lines.push(`Datum: ${dateStr}`);
  lines.push(`Längd: ${formatTime(meta.durationSec)}`);
  if (opts.speakers) {
    const ids = [...new Set(segs.map((s) => s.speaker))];
    lines.push(`Talare: ${ids.map((id) => speakerName(speakers, id)).join(", ")}`);
  }
  if (meta.draft) lines.push("Obs: live-utkast – slutversionen var inte klar vid exporten.");
  lines.push("");
  for (const s of segs) {
    const head: string[] = [];
    if (opts.timestamps) head.push(`[${formatTime(s.start)}]`);
    if (opts.speakers) head.push(`${speakerName(speakers, s.speaker)}:`);
    lines.push(head.length ? `${head.join(" ")}\n${s.text}` : s.text);
    lines.push("");
  }
  return lines.join("\n").replace(/\n+$/, "\n");
}
