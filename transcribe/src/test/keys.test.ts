// Prov av nyckellagringen (keys.ts) och att motorerna använder nyckeln i anropsögonblicket.
// Körs i en egen process: miljön sätts innan modulerna läses in, eftersom inställningar och
// databas läses vid import.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { after, before, test } from "node:test";
import { startMock, tmpDir, type MockService } from "./helpers.js";

const ENV_DG = "prov-deepgram-miljo-0000000000000000";
const UI_DG = "prov-deepgram-nyckel-gransnitt-Qx7Z";
const UI_DG2 = "prov-deepgram-nyckel-andra-Wv3K";
const UI_WH = "prov-whisper-nyckel-gransnitt-Rt5M";
const BAD = "prov-ogiltig-nyckel-1111111111111111";

let dir: string;
let dg: MockService;
let wh: MockService;
let keys: typeof import("../keys.js");
let dbm: typeof import("../db.js");
let deepgram: typeof import("../engines/deepgram.js");
let whisper: typeof import("../engines/whisper.js");
let userId: number;

before(async () => {
  dir = tmpDir();
  dg = await startMock("deepgram");
  wh = await startMock("whisper", "/v1");
  dg.valid.add(UI_DG).add(UI_DG2).add(ENV_DG);
  wh.valid.add(UI_WH);
  for (const k of Object.keys(process.env)) if (k.endsWith("_API_KEY")) delete process.env[k];
  process.env.TRANSCRIBE_DATA_DIR = path.join(dir, "data");
  process.env.DEEPGRAM_API_KEY = ENV_DG;
  process.env.DEEPGRAM_URL = dg.url;
  process.env.WHISPER_API_URL = `${wh.url}/v1`;
  keys = await import("../keys.js");
  dbm = await import("../db.js");
  deepgram = await import("../engines/deepgram.js");
  whisper = await import("../engines/whisper.js");
  userId = Number(
    dbm.db
      .prepare("INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, 'admin')")
      .run("admin@example.com", "Admin", "x").lastInsertRowid,
  );
});

after(async () => {
  dbm?.db.close();
  await dg?.close();
  await wh?.close();
  fs.rmSync(dir, { recursive: true, force: true });
});

const secretFile = () => path.join(dir, "data", "secret.key");

test("kryptering fram och tillbaka; fel namn, fel huvudnyckel och ändrade byte går inte att läsa", () => {
  const master = crypto.randomBytes(32);
  const blob = keys.encryptValue(master, "deepgram", UI_DG);
  assert.equal(keys.decryptValue(master, "deepgram", blob), UI_DG);
  assert.ok(!blob.includes(Buffer.from(UI_DG)), "klartexten finns inte i chiffertexten");
  assert.equal(blob.length, 12 + 16 + Buffer.byteLength(UI_DG));
  // Slumpad IV: samma värde ger olika chiffertext.
  assert.ok(!keys.encryptValue(master, "deepgram", UI_DG).equals(blob));
  assert.throws(() => keys.decryptValue(master, "whisper", blob), "annan rad (AAD)");
  assert.throws(() => keys.decryptValue(crypto.randomBytes(32), "deepgram", blob), "annan huvudnyckel");
  const tampered = Buffer.from(blob);
  tampered[tampered.length - 1]! ^= 1;
  assert.throws(() => keys.decryptValue(master, "deepgram", tampered), "ändrad chiffertext");
});

test("utan sparad nyckel används miljövariabeln och ingen secret.key skapas", () => {
  assert.equal(keys.getKey("deepgram"), ENV_DG);
  assert.equal(keys.getKey("whisper"), "");
  const s = keys.status("deepgram");
  assert.equal(s.source, "env");
  assert.equal(s.set, true);
  assert.equal(s.envSet, true);
  assert.equal(s.hint, "0000");
  assert.equal(s.updatedAt, null);
  assert.equal(s.host, `127.0.0.1:${dg.port}`);
  const w = keys.status("whisper");
  assert.deepEqual({ set: w.set, source: w.source, hint: w.hint, envSet: w.envSet }, {
    set: false,
    source: "none",
    hint: "",
    envSet: false,
  });
  // Adressen pekar inte på groq.com: en egen Whisper-server kräver ingen nyckel.
  assert.equal(w.required, false);
  assert.equal(keys.status("deepgram").required, true);
  assert.equal(fs.existsSync(secretFile()), false);
});

test("sparad nyckel går före miljön, lagras krypterad och secret.key skapas med 0600", () => {
  keys.setKey("deepgram", UI_DG, userId);
  const st = fs.statSync(secretFile());
  assert.equal(st.mode & 0o777, 0o600);
  assert.equal(st.size, 32);
  assert.equal(keys.getKey("deepgram"), UI_DG);
  const s = keys.status("deepgram");
  assert.equal(s.source, "ui");
  assert.equal(s.hint, "Qx7Z");
  assert.equal(s.updatedBy, "admin@example.com");
  assert.ok(s.updatedAt);
  assert.equal(s.unreadable, false);
  assert.equal(s.envSet, true);
  const row = dbm.db.prepare("SELECT ciphertext FROM service_keys WHERE name = 'deepgram'").get() as { ciphertext: Buffer };
  assert.ok(Buffer.isBuffer(row.ciphertext));
  assert.ok(!row.ciphertext.includes(Buffer.from(UI_DG)));
  assert.ok(!row.ciphertext.includes(Buffer.from("Qx7Z")));
  // Hela databasfilen (med WAL) innehåller inte heller klartexten.
  for (const f of fs.readdirSync(path.join(dir, "data"))) {
    if (f.startsWith("transcribe.db")) assert.ok(!fs.readFileSync(path.join(dir, "data", f)).includes(Buffer.from(UI_DG)), f);
  }
});

test("secret.key återanvänds och en ny sparning ger ny chiffertext", () => {
  const before = fs.readFileSync(secretFile());
  const oldBlob = (dbm.db.prepare("SELECT ciphertext FROM service_keys WHERE name = 'deepgram'").get() as { ciphertext: Buffer })
    .ciphertext;
  keys.clearKeyCache();
  assert.equal(keys.getKey("deepgram"), UI_DG, "läses med huvudnyckeln från filen");
  keys.setKey("deepgram", UI_DG2, userId);
  assert.ok(fs.readFileSync(secretFile()).equals(before), "filen skrevs inte om");
  const newBlob = (dbm.db.prepare("SELECT ciphertext FROM service_keys WHERE name = 'deepgram'").get() as { ciphertext: Buffer })
    .ciphertext;
  assert.ok(!newBlob.equals(oldBlob));
  assert.equal(keys.getKey("deepgram"), UI_DG2, "nya värdet gäller direkt (minnet ogiltigförklarat)");
});

test("borttagning: miljövariabeln gäller igen", () => {
  keys.deleteKey("deepgram");
  assert.equal(keys.getKey("deepgram"), ENV_DG);
  assert.equal(keys.status("deepgram").source, "env");
  keys.setKey("deepgram", UI_DG, userId);
  assert.equal(keys.getKey("deepgram"), UI_DG);
});

test("en rad som flyttats till ett annat namn går inte att läsa", () => {
  keys.setKey("whisper", UI_WH, userId);
  const blob = (dbm.db.prepare("SELECT ciphertext FROM service_keys WHERE name = 'deepgram'").get() as { ciphertext: Buffer })
    .ciphertext;
  dbm.db.prepare("UPDATE service_keys SET ciphertext = ? WHERE name = 'whisper'").run(blob);
  keys.clearKeyCache();
  const w = keys.status("whisper");
  assert.equal(w.unreadable, true);
  assert.equal(w.source, "none");
  assert.equal(keys.getKey("whisper"), "");
  keys.deleteKey("whisper");
  assert.equal(keys.status("whisper").unreadable, false);
});

test("motorerna använder den nyckel som gäller i anropsögonblicket", async () => {
  const audio = path.join(dir, "ljud.flac");
  fs.writeFileSync(audio, Buffer.alloc(64));
  assert.equal(whisper.whisperEnabled(), true, "egen Whisper-server fungerar utan nyckel");
  assert.equal(deepgram.deepgramEnabled(), true);

  dg.seen.length = 0;
  keys.setKey("deepgram", UI_DG, userId);
  await deepgram.deepgramFile(audio, "audio/flac");
  keys.setKey("deepgram", UI_DG2, userId);
  await deepgram.deepgramFile(audio, "audio/flac");
  assert.deepEqual(
    dg.seen.filter((r) => r.path === "/v1/listen").map((r) => r.authorization),
    [`Token ${UI_DG}`, `Token ${UI_DG2}`],
  );

  wh.seen.length = 0;
  wh.open = true;
  await whisper.whisperFile(audio, 0, "");
  keys.setKey("whisper", UI_WH, userId);
  await whisper.whisperFile(audio, 0, "");
  assert.deepEqual(
    wh.seen.filter((r) => r.path === "/v1/audio/transcriptions").map((r) => r.authorization),
    ["", `Bearer ${UI_WH}`],
  );

  // Nekad nyckel: begripligt fel som hänvisar till Administration och inte innehåller nyckeln.
  keys.setKey("whisper", BAD, userId);
  await assert.rejects(whisper.whisperFile(audio, 0, ""), (e: Error) => {
    assert.match(e.message, /nekade nyckeln\. Kontrollera den under Administration\./);
    assert.ok(!e.message.includes(BAD));
    return true;
  });
  keys.setKey("deepgram", BAD, userId);
  await assert.rejects(deepgram.deepgramFile(audio, "audio/flac"), (e: Error) => {
    assert.match(e.message, /^Deepgram nekade nyckeln\. Kontrollera den under Administration\.$/);
    return true;
  });
  wh.open = false;
  keys.deleteKey("whisper");
  keys.setKey("deepgram", UI_DG, userId);
});

test("kontrollen: godkänd, nekad, annan status, omdirigering, nätfel", async () => {
  dg.mode = "normal";
  assert.deepEqual(await keys.checkKey("deepgram", UI_DG), { result: "ok", detail: "Nyckeln fungerar." });
  const denied = await keys.checkKey("deepgram", BAD);
  assert.equal(denied.result, "denied");
  assert.ok(!JSON.stringify(denied).includes("Authentication failed"), "tjänstens svarstext tas inte med");
  assert.equal((await keys.checkKey("whisper", UI_WH)).result, "ok");
  assert.equal((await keys.checkKey("whisper", BAD)).result, "denied");
  assert.equal(dg.seen.at(-1)!.path, "/v1/projects");
  assert.equal(wh.seen.at(-1)!.path, "/v1/models");
  assert.equal(wh.seen.at(-1)!.authorization, `Bearer ${BAD}`);

  dg.mode = "503";
  assert.deepEqual(await keys.checkKey("deepgram", UI_DG), { result: "unknown", detail: "Deepgram svarade 503." });
  dg.mode = "403";
  assert.deepEqual(await keys.checkKey("deepgram", UI_DG), { result: "unknown", detail: "Deepgram svarade 403." });
  dg.mode = "redirect";
  const before = dg.seen.length;
  assert.deepEqual(await keys.checkKey("deepgram", UI_DG), { result: "unknown", detail: "Deepgram svarade 302." });
  assert.equal(dg.seen.length, before + 1, "omdirigeringen följdes inte");
  dg.mode = "drop";
  assert.deepEqual(await keys.checkKey("deepgram", UI_DG), { result: "unknown", detail: "Deepgram gick inte att nå." });
  dg.mode = "normal";

  assert.deepEqual(await keys.testKey("whisper"), { result: "missing", detail: "Ingen nyckel är inlagd." });
  assert.equal((await keys.testKey("deepgram")).result, "ok");
});

test("kontrollen ger upp efter 10 sekunder", { timeout: 20_000 }, async () => {
  dg.mode = "hang";
  const t = Date.now();
  const r = await keys.checkKey("deepgram", UI_DG);
  dg.mode = "normal";
  assert.deepEqual(r, { result: "unknown", detail: "Deepgram svarade inte inom 10 sekunder." });
  assert.ok(Date.now() - t >= 9_500);
});

test("ny secret.key: sparade nycklar blir oläsliga och miljön gäller", () => {
  keys.setKey("deepgram", UI_DG, userId);
  fs.writeFileSync(secretFile(), crypto.randomBytes(32), { mode: 0o600 });
  keys.clearKeyCache();
  const s = keys.status("deepgram");
  assert.equal(s.unreadable, true);
  assert.equal(s.source, "env");
  assert.equal(keys.getKey("deepgram"), ENV_DG);
  // Att spara igen fungerar med den nya huvudnyckeln.
  keys.setKey("deepgram", UI_DG, userId);
  assert.equal(keys.status("deepgram").unreadable, false);
  assert.equal(keys.getKey("deepgram"), UI_DG);
});

test("skadad secret.key: skrivs aldrig över, sparning nekas, nycklar räknas som oläsliga", () => {
  fs.writeFileSync(secretFile(), Buffer.from("för kort"));
  keys.clearKeyCache();
  assert.throws(() => keys.ensureWritable(), keys.KeyStoreError);
  assert.throws(() => keys.setKey("whisper", UI_WH, userId), (e: Error) => {
    assert.ok(e instanceof keys.KeyStoreError);
    assert.match(e.message, /secret\.key/);
    assert.ok(!e.message.includes(UI_WH));
    return true;
  });
  assert.equal(fs.readFileSync(secretFile(), "utf8"), "för kort");
  const s = keys.status("deepgram");
  assert.equal(s.unreadable, true);
  assert.equal(keys.getKey("deepgram"), ENV_DG);
  assert.equal((dbm.db.prepare("SELECT COUNT(*) AS n FROM service_keys WHERE name = 'whisper'").get() as { n: number }).n, 0);
});

test("validering av inklistrad nyckel", () => {
  assert.deepEqual(keys.validateKey(`  ${UI_DG}\n`), { value: UI_DG });
  for (const bad of [undefined, 123, "", "   ", "kort", "a".repeat(513), "har mellanslag i sig", "nyckel\tmed-tab", "åäö-nyckel-123"]) {
    assert.ok("error" in keys.validateKey(bad), JSON.stringify(bad));
  }
  assert.deepEqual(keys.validateKey("a".repeat(512)), { value: "a".repeat(512) });
  assert.deepEqual(keys.validateKey("12345678"), { value: "12345678" });
});
