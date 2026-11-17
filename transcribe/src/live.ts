// Live-inspelning: webbläsaren skickar 16 kHz PCM över WebSocket. Servern sparar ljudet,
// vidarebefordrar det till Deepgram och skickar tillbaka live-text med talare.
// Bryts anslutningen kan webbläsaren återansluta och fortsätta i samma inspelning.
import fs from "node:fs";
import path from "node:path";
import type { FastifyBaseLogger, FastifyInstance } from "fastify";
import type { WebSocket } from "ws";
import { WavWriter } from "./audio.js";
import { sameOrigin } from "./auth.js";
import { config } from "./config.js";
import { addUsage, audioDir, db, now, parseJson, reindex, type Segment, type TranscriptRow } from "./db.js";
import { DeepgramLive, deepgramEnabled } from "./engines/deepgram.js";
import { enqueue } from "./jobs.js";
import { wordsToSegments, type Word } from "./merge.js";

const active = new Map<string, LiveSession>();

export function isLive(id: string): boolean {
  return active.has(id);
}

function send(socket: WebSocket, msg: unknown): void {
  if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(msg));
}

class LiveSession {
  private wav: WavWriter;
  private dg: DeepgramLive | null = null;
  private baseSegments: Segment[];
  private words: Word[] = [];
  private sentCount = 0;
  private persistTimer: NodeJS.Timeout;
  private startSeconds: number;
  private stopping = false;
  private closed = false;
  private dgFailed = false;

  constructor(
    readonly id: string,
    private userId: number,
    private socket: WebSocket,
    private log: FastifyBaseLogger,
  ) {
    const row = db.prepare("SELECT live_segments FROM transcripts WHERE id = ?").get(id) as Pick<
      TranscriptRow,
      "live_segments"
    >;
    this.baseSegments = parseJson<Segment[]>(row.live_segments, []);
    const dir = audioDir(id);
    fs.mkdirSync(dir, { recursive: true });
    this.wav = new WavWriter(path.join(dir, "recording.wav"));
    this.startSeconds = this.wav.seconds;
    this.sentCount = Math.max(0, this.baseSegments.length - 1);

    if (deepgramEnabled()) this.connectDeepgram();
    send(socket, {
      type: "ready",
      live: deepgramEnabled(),
      offset: this.startSeconds,
      segments: this.baseSegments,
      maxSeconds: config.maxRecordingMinutes * 60,
    });

    this.persistTimer = setInterval(() => this.persist(), 10_000);
    socket.on("message", (data: Buffer, isBinary: boolean) => this.onMessage(data, isBinary));
    socket.on("close", () => void this.onSocketClose());
    socket.on("error", () => void this.onSocketClose());
  }

  private connectDeepgram(): void {
    this.dg = new DeepgramLive(this.wav.seconds, {
      onFinal: (words) => {
        this.words.push(...words);
        this.pushSegments();
      },
      onInterim: (words) => send(this.socket, { type: "interim", words }),
      onError: (message) => {
        this.dgFailed = true;
        this.log.warn({ id: this.id, message }, "Deepgram live-fel");
        send(this.socket, { type: "notice", message: `Live-texten är avbruten: ${message} Ljudet spelas fortfarande in.` });
      },
      onClose: () => {
        // Oväntat stängd (nätverk o.d.) mitt i inspelningen: koppla upp igen en gång per 5 s.
        if (!this.stopping && !this.closed && !this.dgFailed) {
          setTimeout(() => {
            if (this.stopping || this.closed || this.dgFailed) return;
            // Nyckeln kan ha tagits bort under Administration medan inspelningen pågick.
            if (!deepgramEnabled()) {
              this.dgFailed = true;
              this.log.warn({ id: this.id }, "Deepgram-nyckeln saknas, live-texten kopplas inte upp igen");
              send(this.socket, {
                type: "notice",
                message: "Live-texten är avstängd eftersom Deepgram-nyckeln togs bort. Ljudet spelas fortfarande in.",
              });
              return;
            }
            this.connectDeepgram();
          }, 5000);
        }
      },
    });
  }

  private segments(): Segment[] {
    const fresh = wordsToSegments(this.words);
    return [...this.baseSegments, ...fresh];
  }

  /** Skickar de stycken som ändrats sedan sist (sista stycket kan ha vuxit). */
  private pushSegments(): void {
    const all = this.segments();
    const from = Math.min(this.sentCount, Math.max(0, all.length - 1));
    send(this.socket, { type: "segments", from, segments: all.slice(from) });
    this.sentCount = Math.max(0, all.length - 1);
  }

  private onMessage(data: Buffer, isBinary: boolean): void {
    if (this.stopping) return;
    if (isBinary) {
      this.wav.write(data);
      this.dg?.send(data);
      if (this.wav.seconds >= config.maxRecordingMinutes * 60) {
        send(this.socket, { type: "notice", message: `Inspelningen stoppades vid maxlängden ${config.maxRecordingMinutes} min.` });
        void this.stop();
      }
      return;
    }
    let msg: { type?: string };
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (msg.type === "stop") void this.stop();
    else if (msg.type === "ping") send(this.socket, { type: "pong", seconds: this.wav.seconds });
  }

  private persist(): void {
    if (this.closed) return;
    try {
      this.wav.flushHeader();
      db.prepare(
        "UPDATE transcripts SET live_segments = ?, duration_sec = ?, audio_bytes = ?, last_activity = ?, updated_at = ? WHERE id = ?",
      ).run(JSON.stringify(this.segments()), this.wav.seconds, this.wav.dataBytes, now(), now(), this.id);
    } catch (e) {
      this.log.error({ err: e, id: this.id }, "Kunde inte spara live-läget");
    }
  }

  private finishBookkeeping(): void {
    clearInterval(this.persistTimer);
    this.persist();
    if (this.dg) addUsage(this.userId, "deepgram-live", this.wav.seconds - this.startSeconds);
    this.closed = true;
    this.wav.close();
    active.delete(this.id);
  }

  async stop(): Promise<void> {
    if (this.stopping || this.closed) return;
    this.stopping = true;
    send(this.socket, { type: "stopping" });
    if (this.dg) {
      await this.dg.finish();
      this.pushSegments();
    }
    this.finishBookkeeping();
    db.prepare("UPDATE transcripts SET status = 'processing', status_detail = 'I kö', updated_at = ? WHERE id = ?").run(
      now(),
      this.id,
    );
    reindex(this.id);
    enqueue(this.id);
    send(this.socket, { type: "stopped", id: this.id });
    this.socket.close(1000, "klar");
  }

  /** Webbläsaren försvann utan att stoppa: spara läget, inspelningen kan fortsätta senare. */
  private async onSocketClose(): Promise<void> {
    if (this.stopping || this.closed) return;
    this.dg?.abort();
    this.finishBookkeeping();
  }

  /** En ny anslutning till samma inspelning tar över (t.ex. efter nätverksavbrott). */
  takeOver(): void {
    if (this.closed) return;
    this.dg?.abort();
    this.finishBookkeeping();
    try {
      this.socket.close(4000, "övertagen");
    } catch {
      /* ignorera */
    }
  }
}

export function registerLive(app: FastifyInstance): void {
  app.get<{ Params: { id: string } }>("/api/live/:id", { websocket: true }, (socket, req) => {
    const user = req.user;
    if (!user || !sameOrigin(req)) {
      socket.close(4401, "Inte inloggad");
      return;
    }
    const row = db.prepare("SELECT id, user_id, status FROM transcripts WHERE id = ?").get(req.params.id) as
      | Pick<TranscriptRow, "id" | "user_id" | "status">
      | undefined;
    if (!row || row.user_id !== user.id) {
      socket.close(4404, "Finns inte");
      return;
    }
    if (row.status !== "recording") {
      socket.close(4409, "Inspelningen är avslutad");
      return;
    }
    active.get(row.id)?.takeOver();
    if (active.size >= config.maxConcurrentRecordings) {
      socket.close(4429, "För många samtidiga inspelningar");
      return;
    }
    active.set(row.id, new LiveSession(row.id, user.id, socket, req.log));
  });

  // Inspelningar som övergetts (webbläsaren stängd) avslutas efter 10 minuter utan aktivitet.
  setInterval(() => {
    const cutoff = new Date(Date.now() - 10 * 60_000).toISOString();
    const stale = db
      .prepare("SELECT id FROM transcripts WHERE status = 'recording' AND last_activity < ?")
      .all(cutoff) as Array<{ id: string }>;
    for (const s of stale) {
      if (active.has(s.id)) continue;
      db.prepare("UPDATE transcripts SET status = 'processing', status_detail = 'I kö', updated_at = ? WHERE id = ?").run(
        now(),
        s.id,
      );
      enqueue(s.id);
    }
  }, 60_000).unref();
}
