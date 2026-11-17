// Prov av terminalkommandot "upptagen" som den automatiska uppdateringen frågar innan omstart.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { after, test } from "node:test";
import Database from "better-sqlite3";
import { cleanEnv, ROOT, tmpDir } from "./helpers.js";

const dir = tmpDir();
const dataDir = path.join(dir, "data");

after(() => fs.rmSync(dir, { recursive: true, force: true }));

function cli(...args: string[]) {
  const r = spawnSync(process.execPath, [path.join(ROOT, "dist", "cli.js"), ...args], {
    cwd: ROOT,
    env: cleanEnv({ TRANSCRIBE_DATA_DIR: dataDir }),
    encoding: "utf8",
  });
  return { code: r.status, out: r.stdout, err: r.stderr };
}

function addTranscript(db: Database.Database, id: string, status: string, minutesAgo: number): void {
  const t = new Date(Date.now() - minutesAgo * 60_000).toISOString();
  db.prepare("INSERT INTO transcripts (id, user_id, title, status, last_activity) VALUES (?, 1, ?, ?, ?)").run(id, id, status, t);
}

test("upptagen: ledig, pågående inspelning och bearbetning", () => {
  assert.deepEqual(cli("upptagen"), { code: 0, out: "ledig\n", err: "" });

  const db = new Database(path.join(dataDir, "transcribe.db"));
  db.prepare("INSERT INTO users (id, email, password_hash, role) VALUES (1, 'a@example.com', 'x', 'admin')").run();
  // Övergiven inspelning (ingen aktivitet på 10 minuter) och färdiga räknas inte.
  addTranscript(db, "gammal-inspelning", "recording", 10);
  addTranscript(db, "klar-transkribering", "ready", 0);
  addTranscript(db, "misslyckad-transkr", "failed", 0);
  assert.deepEqual(cli("upptagen"), { code: 0, out: "ledig\n", err: "" });

  addTranscript(db, "pagaende-inspelning", "recording", 1);
  assert.deepEqual(cli("upptagen"), { code: 10, out: "upptagen: 1 inspelning pågår\n", err: "" });

  addTranscript(db, "bearbetas-nummer-1", "processing", 30);
  addTranscript(db, "bearbetas-nummer-2", "processing", 0);
  assert.deepEqual(cli("upptagen"), { code: 10, out: "upptagen: 1 inspelning pågår, 2 väntar på slutversion\n", err: "" });

  addTranscript(db, "pagaende-nummer-2", "recording", 0);
  db.prepare("DELETE FROM transcripts WHERE status = 'processing'").run();
  assert.deepEqual(cli("upptagen"), { code: 10, out: "upptagen: 2 inspelningar pågår\n", err: "" });

  db.prepare("DELETE FROM transcripts WHERE status = 'recording'").run();
  addTranscript(db, "bearbetas-nummer-3", "processing", 0);
  assert.deepEqual(cli("upptagen"), { code: 10, out: "upptagen: 1 väntar på slutversion\n", err: "" });
  db.close();
});

test("okänt kommando ger kod 1, inget kommando kod 0", () => {
  assert.equal(cli("finns-inte").code, 1);
  const none = cli();
  assert.equal(none.code, 0);
  assert.match(none.out, /upptagen/);
});
