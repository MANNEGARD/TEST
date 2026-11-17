// Prov av bearbetningskön när nycklar saknas, nekas eller tjänsten svarar med fel: felet som
// sparas på transkriberingen (och visas för den som äger den) ska hänvisa till Administration
// och aldrig innehålla nyckeln. Körs i en egen process med en egen datakatalog och låtsasservrar.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { after, before, test } from "node:test";
import { startMock, tmpDir, type MockService } from "./helpers.js";

const DG_KEY = "prov-deepgram-nyckel-jobb-0000000000";
const WH_KEY = "prov-groq-nyckel-jobb-00000000000000";

let dir: string;
let dg: MockService;
let wh: MockService;
let keys: typeof import("../keys.js");
let dbm: typeof import("../db.js");
let jobs: typeof import("../jobs.js");
let deepgram: typeof import("../engines/deepgram.js");
let whisper: typeof import("../engines/whisper.js");
let audio: typeof import("../audio.js");
let userId: number;
let counter = 0;

before(async () => {
  dir = tmpDir();
  dg = await startMock("deepgram");
  // Adressen innehåller "groq.com": nyckel krävs, och tjänsten heter Groq i meddelandena.
  wh = await startMock("whisper", "/groq.com/openai/v1");
  for (const k of Object.keys(process.env)) if (k.endsWith("_API_KEY")) delete process.env[k];
  process.env.TRANSCRIBE_DATA_DIR = path.join(dir, "data");
  process.env.DEEPGRAM_URL = dg.url;
  process.env.WHISPER_API_URL = `${wh.url}/groq.com/openai/v1`;
  keys = await import("../keys.js");
  dbm = await import("../db.js");
  jobs = await import("../jobs.js");
  deepgram = await import("../engines/deepgram.js");
  whisper = await import("../engines/whisper.js");
  audio = await import("../audio.js");
  keys.setKeyLogger({ warn: () => undefined });
  // Tyst logg i proven (kön är tom, så inget jobb tas upp här).
  const quiet = () => undefined;
  jobs.initJobs({ info: quiet, warn: quiet, error: quiet } as unknown as Parameters<typeof jobs.initJobs>[0]);
  userId = Number(
    dbm.db
      .prepare("INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, 'user')")
      .run("anvandare@example.com", "Användare", "x").lastInsertRowid,
  );
});

after(async () => {
  dbm?.db.close();
  await dg?.close();
  await wh?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

/** En transkribering med två sekunder tyst inspelning, redo för slutversionen. */
function newRecording(): string {
  const id = `jobbprov-${++counter}-abcdef`;
  dbm.db
    .prepare("INSERT INTO transcripts (id, user_id, title, status, last_activity) VALUES (?, ?, ?, 'processing', ?)")
    .run(id, userId, id, dbm.now());
  const d = dbm.audioDir(id);
  fs.mkdirSync(d, { recursive: true });
  const w = new audio.WavWriter(path.join(d, "recording.wav"));
  w.write(Buffer.alloc(16000 * 2 * 2));
  w.close();
  return id;
}

async function finished(id: string): Promise<{ status: string; error: string }> {
  const deadline = Date.now() + 30_000;
  for (;;) {
    const row = dbm.db.prepare("SELECT status, error FROM transcripts WHERE id = ?").get(id) as { status: string; error: string };
    if (row.status !== "processing") return row;
    if (Date.now() > deadline) throw new Error("Jobbet blev inte klart");
    await new Promise((r) => setTimeout(r, 100));
  }
}

test("motorerna anropar inte tjänsten utan nyckel utan ger ett eget fel", async () => {
  const file = path.join(dir, "bit.flac");
  fs.writeFileSync(file, Buffer.alloc(64));
  const before = dg.seen.length + wh.seen.length;
  await assert.rejects(deepgram.deepgramFile(file, "audio/flac"), (e: Error) => {
    assert.ok(e instanceof keys.MissingKeyError);
    assert.equal(e.message, "Deepgram-nyckeln saknas. Lägg in den under Administration.");
    return true;
  });
  await assert.rejects(whisper.whisperFile(file, 0, ""), (e: Error) => {
    assert.ok(e instanceof keys.MissingKeyError);
    assert.equal(e.message, "Groq-nyckeln saknas. Lägg in den under Administration.");
    return true;
  });
  assert.equal(dg.seen.length + wh.seen.length, before, "ingen begäran utan nyckel");
});

test("utan nycklar: transkriberingen misslyckas med en hänvisning till Administration", async () => {
  const id = newRecording();
  jobs.enqueue(id);
  const row = await finished(id);
  assert.equal(row.status, "failed");
  assert.equal(row.error, "Ingen transkriberingstjänst är inställd. Lägg in nycklarna under Administration.");
});

test("nekad nyckel och fel från tjänsten: begripligt fel med samma namn som under Administration, utan nyckeln", async () => {
  keys.setKey("deepgram", DG_KEY, userId);
  keys.setKey("whisper", WH_KEY, userId); // Groq-låtsasen godtar den inte: 401
  dg.mode = "echo"; // 500 med Authorization-huvudet i svaret
  try {
    const id = newRecording();
    jobs.enqueue(id);
    const row = await finished(id);
    assert.equal(row.status, "failed");
    assert.equal(
      row.error,
      "Talaruppdelningen misslyckades: Deepgram svarade 500. " +
        "Whisper misslyckades: Groq nekade nyckeln. Kontrollera den under Administration.",
    );
    assert.ok(!row.error.includes(DG_KEY) && !row.error.includes(WH_KEY));
    // En nekad nyckel försöks inte igen.
    assert.equal(wh.seen.filter((r) => r.path.endsWith("/audio/transcriptions")).length, 1);
  } finally {
    dg.mode = "normal";
  }
});
