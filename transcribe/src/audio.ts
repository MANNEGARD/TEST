// Ljudhantering: inspelningen sparas som 16 kHz mono 16-bitars WAV medan den pågår
// (enkelt att fortsätta efter ett avbrott), och konverteras med ffmpeg efteråt.
import { spawn } from "node:child_process";
import fs from "node:fs";

export const SAMPLE_RATE = 16000;
export const BYTES_PER_SECOND = SAMPLE_RATE * 2;
const HEADER = 44;

function wavHeader(dataBytes: number): Buffer {
  const b = Buffer.alloc(HEADER);
  b.write("RIFF", 0, "ascii");
  b.writeUInt32LE(Math.min(36 + dataBytes, 0xffffffff), 4);
  b.write("WAVE", 8, "ascii");
  b.write("fmt ", 12, "ascii");
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20); // PCM
  b.writeUInt16LE(1, 22); // mono
  b.writeUInt32LE(SAMPLE_RATE, 24);
  b.writeUInt32LE(BYTES_PER_SECOND, 28);
  b.writeUInt16LE(2, 32);
  b.writeUInt16LE(16, 34);
  b.write("data", 36, "ascii");
  b.writeUInt32LE(Math.min(dataBytes, 0xffffffff), 40);
  return b;
}

/** Skriver PCM till en WAV-fil. Kan öppna en befintlig fil och fortsätta på slutet. */
export class WavWriter {
  private fd: number;
  dataBytes: number;

  constructor(readonly file: string) {
    if (fs.existsSync(file)) {
      this.fd = fs.openSync(file, "r+");
      const size = fs.fstatSync(this.fd).size;
      // Jämnt antal byte (hela 16-bitars sampel) efter huvudet.
      this.dataBytes = Math.max(0, size - HEADER) & ~1;
    } else {
      this.fd = fs.openSync(file, "w+");
      fs.writeSync(this.fd, wavHeader(0), 0, HEADER, 0);
      this.dataBytes = 0;
    }
  }

  get seconds(): number {
    return this.dataBytes / BYTES_PER_SECOND;
  }

  write(pcm: Buffer): void {
    const len = pcm.length & ~1;
    if (len === 0) return;
    fs.writeSync(this.fd, pcm, 0, len, HEADER + this.dataBytes);
    this.dataBytes += len;
  }

  /** Uppdaterar huvudet så att filen är giltig även om processen dör senare. */
  flushHeader(): void {
    fs.writeSync(this.fd, wavHeader(this.dataBytes), 0, HEADER, 0);
  }

  close(): void {
    try {
      this.flushHeader();
    } finally {
      fs.closeSync(this.fd);
    }
  }
}

/** Gör huvudet i en WAV-fil korrekt utifrån filstorleken (efter en krasch). */
export function repairWav(file: string): number {
  const w = new WavWriter(file);
  const bytes = w.dataBytes;
  w.close();
  return bytes;
}

function run(cmd: string, args: string[], timeoutMs = 60 * 60_000): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => p.kill("SIGKILL"), timeoutMs);
    p.stdout.on("data", (d: Buffer) => {
      if (stdout.length < 5_000_000) stdout += d.toString();
    });
    p.stderr.on("data", (d: Buffer) => {
      stderr += d.toString();
      if (stderr.length > 2_000_000) stderr = stderr.slice(-1_000_000);
    });
    p.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    p.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${cmd} avslutades med kod ${code}: ${stderr.slice(-500)}`));
    });
  });
}

const FF = ["-hide_banner", "-nostdin", "-loglevel", "info", "-y"];

/** Komprimerad kopia för uppspelning i webbläsaren (AAC i M4A fungerar överallt). */
export async function toM4a(input: string, output: string): Promise<void> {
  await run("ffmpeg", [...FF, "-i", input, "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", output]);
}

/** Förlustfritt 16 kHz mono-utdrag (FLAC) för Whisper. */
export async function extractFlac(input: string, output: string, start: number, duration: number): Promise<void> {
  await run("ffmpeg", [
    ...FF,
    "-ss",
    start.toFixed(3),
    "-t",
    duration.toFixed(3),
    "-i",
    input,
    "-ac",
    "1",
    "-ar",
    String(SAMPLE_RATE),
    "-c:a",
    "flac",
    output,
  ]);
}

/** Konverterar vilken ljudfil som helst till 16 kHz mono WAV. */
export async function toWav(input: string, output: string): Promise<void> {
  await run("ffmpeg", [...FF, "-i", input, "-ac", "1", "-ar", String(SAMPLE_RATE), "-c:a", "pcm_s16le", output]);
}

export async function probeDuration(file: string): Promise<number> {
  const { stdout } = await run("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "default=noprint_wrappers=1:nokey=1",
    file,
  ]);
  const d = Number.parseFloat(stdout.trim());
  return Number.isFinite(d) ? d : 0;
}

export interface Silence {
  start: number;
  end: number;
}

/** Hittar tysta partier (för att dela ljudet där ingen pratar). */
export async function detectSilences(file: string): Promise<Silence[]> {
  const { stderr } = await run("ffmpeg", [
    "-hide_banner",
    "-nostdin",
    "-i",
    file,
    "-af",
    "silencedetect=noise=-35dB:d=0.35",
    "-f",
    "null",
    "-",
  ]);
  return parseSilences(stderr);
}

export function parseSilences(log: string): Silence[] {
  const out: Silence[] = [];
  let start: number | null = null;
  for (const line of log.split("\n")) {
    const s = /silence_start:\s*(-?[\d.]+)/.exec(line);
    if (s) start = Math.max(0, Number.parseFloat(s[1]!));
    const e = /silence_end:\s*([\d.]+)/.exec(line);
    if (e && start !== null) {
      out.push({ start, end: Number.parseFloat(e[1]!) });
      start = null;
    }
  }
  return out;
}

/**
 * Väljer var ljudet ska delas: så nära målängden som möjligt men i mitten av en tystnad,
 * så att inga ord klipps av. Hittas ingen tystnad i fönstret delas det vid maxlängden.
 */
export function planChunks(duration: number, silences: Silence[], target: number): Array<{ start: number; end: number }> {
  const chunks: Array<{ start: number; end: number }> = [];
  let pos = 0;
  const minLen = target * 0.6;
  while (duration - pos > target) {
    const windowStart = pos + minLen;
    const windowEnd = pos + target;
    let best: number | null = null;
    let bestScore = -Infinity;
    for (const s of silences) {
      const mid = (s.start + s.end) / 2;
      if (mid <= windowStart || mid >= windowEnd) continue;
      // Längre tystnad och senare position (färre anrop) ger högre poäng.
      const score = Math.min(s.end - s.start, 2) * 10 + (mid - windowStart) / target;
      if (score > bestScore) {
        bestScore = score;
        best = mid;
      }
    }
    const cut = best ?? windowEnd;
    chunks.push({ start: pos, end: cut });
    pos = cut;
  }
  if (duration - pos > 0.05 || chunks.length === 0) chunks.push({ start: pos, end: duration });
  return chunks;
}
