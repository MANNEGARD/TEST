// SQLite-databasen. Ett enda filbaserat lager räcker gott för en liten VPS och gör
// säkerhetskopiering enkel (kopiera data-katalogen).
import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import { config } from "./config.js";

export type Role = "admin" | "user";
export type Status = "recording" | "processing" | "ready" | "failed";

export interface Segment {
  /** Talare som sträng-id ("0", "1", …). Namn finns i transcript.speakers. */
  speaker: string;
  start: number;
  end: number;
  text: string;
}

export interface UserRow {
  id: number;
  email: string;
  name: string;
  password_hash: string;
  role: Role;
  disabled: number;
  created_at: string;
}

export interface TranscriptRow {
  id: string;
  user_id: number;
  folder_id: number | null;
  title: string;
  status: Status;
  status_detail: string;
  error: string;
  source: string;
  context: string;
  duration_sec: number;
  audio_bytes: number;
  has_audio: number;
  speakers: string; // JSON: { "0": "Anna" }
  live_segments: string; // JSON: Segment[]
  segments: string; // JSON: Segment[] (slutversion)
  final_engine: string;
  tags: string; // JSON: string[]
  created_at: string;
  updated_at: string;
  last_activity: string;
}

const MIGRATIONS: string[] = [
  `
  CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','user')),
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  );
  CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_agent TEXT NOT NULL DEFAULT ''
  );
  CREATE INDEX sessions_user ON sessions(user_id);
  CREATE TABLE invites (
    token_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin','user')),
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  );
  CREATE TABLE folders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (user_id, name)
  );
  CREATE TABLE transcripts (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    status_detail TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'mic',
    context TEXT NOT NULL DEFAULT '',
    duration_sec REAL NOT NULL DEFAULT 0,
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    has_audio INTEGER NOT NULL DEFAULT 0,
    speakers TEXT NOT NULL DEFAULT '{}',
    live_segments TEXT NOT NULL DEFAULT '[]',
    segments TEXT NOT NULL DEFAULT '[]',
    final_engine TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_activity TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  );
  CREATE INDEX transcripts_user ON transcripts(user_id, created_at);
  CREATE TABLE usage (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,
    month TEXT NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, engine, month)
  );
  -- Fritextsökning över titel och text (svenska tecken: unicode61 utan diakritborttagning
  -- så att å/ä/ö förblir egna bokstäver).
  CREATE VIRTUAL TABLE transcripts_fts USING fts5(
    id UNINDEXED, title, body, tokenize = "unicode61 remove_diacritics 0"
  );
  `,
];

function migrate(db: Database.Database): void {
  const current = db.pragma("user_version", { simple: true }) as number;
  for (let i = current; i < MIGRATIONS.length; i++) {
    db.transaction(() => {
      db.exec(MIGRATIONS[i]!);
      db.pragma(`user_version = ${i + 1}`);
    })();
  }
}

export function openDb(file = path.join(config.dataDir, "transcribe.db")): Database.Database {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const db = new Database(file);
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  db.pragma("busy_timeout = 5000");
  migrate(db);
  return db;
}

export const db = openDb(process.env.TRANSCRIBE_DB_FILE || undefined);

export function now(): string {
  return new Date().toISOString();
}

export function parseJson<T>(raw: string, fallback: T): T {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/** Uppdaterar sökindexet för en transkribering (titel + bästa tillgängliga text). */
export function reindex(id: string): void {
  const row = db.prepare("SELECT title, segments, live_segments FROM transcripts WHERE id = ?").get(id) as
    | Pick<TranscriptRow, "title" | "segments" | "live_segments">
    | undefined;
  db.prepare("DELETE FROM transcripts_fts WHERE id = ?").run(id);
  if (!row) return;
  let segs = parseJson<Segment[]>(row.segments, []);
  if (segs.length === 0) segs = parseJson<Segment[]>(row.live_segments, []);
  const body = segs.map((s) => s.text).join("\n");
  db.prepare("INSERT INTO transcripts_fts (id, title, body) VALUES (?, ?, ?)").run(id, row.title, body);
}

export function addUsage(userId: number, engine: string, seconds: number): void {
  if (!(seconds > 0)) return;
  const month = new Date().toISOString().slice(0, 7);
  db.prepare(
    `INSERT INTO usage (user_id, engine, month, seconds) VALUES (?, ?, ?, ?)
     ON CONFLICT (user_id, engine, month) DO UPDATE SET seconds = seconds + excluded.seconds`,
  ).run(userId, engine, month, seconds);
}

export function audioDir(id: string): string {
  if (!/^[a-zA-Z0-9_-]{8,64}$/.test(id)) throw new Error("Ogiltigt id");
  return path.join(config.dataDir, "audio", id);
}
