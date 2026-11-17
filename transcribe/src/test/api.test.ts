// Prov av API:t för nycklarna, /api/health och ursprungskontrollen. Den byggda servern startas
// som egen process med en tillfällig datakatalog och låtsasservrar för Deepgram och Groq.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { after, before, test } from "node:test";
import WebSocket from "ws";
import {
  allResponses,
  firstAdmin,
  invitedAccount,
  request,
  ROOT,
  startMock,
  startServer,
  tmpDir,
  type MockService,
  type RunningServer,
} from "./helpers.js";

const DG_OK = "prov-deepgram-nyckel-giltig-Qx7Z";
const DG_OK2 = "prov-deepgram-nyckel-andra-Wv3K";
const DG_BAD = "prov-deepgram-nyckel-ogiltig-Zz9Y";
const WH_OK = "prov-groq-nyckel-giltig-Rt5M";
const ALL_KEYS = [DG_OK, DG_OK2, DG_BAD, WH_OK];
const HINTS = ["Qx7Z", "Wv3K", "Zz9Y", "Rt5M"];
// Adressen innehåller "groq.com" så att tjänsten behandlas som Groq (nyckel krävs).
const GROQ_PREFIX = "/groq.com/openai/v1";

let dir: string;
let dg: MockService;
let wh: MockService;
let server: RunningServer;
let admin: string;
let user: string;

before(async () => {
  dir = tmpDir();
  dg = await startMock("deepgram");
  wh = await startMock("whisper", GROQ_PREFIX);
  dg.valid.add(DG_OK).add(DG_OK2);
  wh.valid.add(WH_OK);
  server = await startServer(path.join(dir, "data"), {
    DEEPGRAM_URL: dg.url,
    WHISPER_API_URL: `${wh.url}${GROQ_PREFIX}`,
  });
  admin = await firstAdmin(server);
  user = await invitedAccount(server.url, admin, "user");
});

after(async () => {
  await server?.stop();
  await dg?.close();
  await wh?.close();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
});

const api = (p: string, opts: Parameters<typeof request>[2] = {}) => request(server.url, p, opts);
const features = async (cookie: string) => (await api("/api/me", { cookie })).json.features;

test("/api/health svarar med versionen i package.json utan inloggning", async () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8")) as { version: string };
  const res = await api("/api/health");
  assert.equal(res.status, 200);
  assert.deepEqual(res.json, { ok: true, version: pkg.version });
  assert.equal(pkg.version, "1.1.0");
  assert.equal(res.headers["cache-control"], "no-store");
});

test("alla fyra vägarna kräver inloggning (401) och administratör (403)", async () => {
  const routes: Array<[string, string, unknown?]> = [
    ["GET", "/api/admin/keys"],
    ["PUT", "/api/admin/keys/deepgram", { key: DG_OK }],
    ["DELETE", "/api/admin/keys/deepgram"],
    ["POST", "/api/admin/keys/deepgram/test", {}],
  ];
  const seenBefore = dg.seen.length;
  for (const [method, p, body] of routes) {
    const anon = await api(p, { method, body });
    assert.equal(anon.status, 401, `${method} ${p} utan inloggning`);
    const plain = await api(p, { method, body, cookie: user });
    assert.equal(plain.status, 403, `${method} ${p} som vanlig användare`);
    assert.equal(plain.headers["cache-control"], "no-store");
  }
  assert.equal(dg.seen.length, seenBefore, "inga anrop till Deepgram");
});

test("utan nycklar: läget visas och tjänsterna är av", async () => {
  const res = await api("/api/admin/keys", { cookie: admin });
  assert.equal(res.status, 200);
  assert.equal(res.headers["cache-control"], "no-store");
  assert.deepEqual(res.json.keys, [
    {
      name: "deepgram",
      set: false,
      source: "none",
      hint: "",
      updatedAt: null,
      updatedBy: null,
      unreadable: false,
      envSet: false,
      required: true,
      host: `127.0.0.1:${dg.port}`,
    },
    {
      name: "whisper",
      set: false,
      source: "none",
      hint: "",
      updatedAt: null,
      updatedBy: null,
      unreadable: false,
      envSet: false,
      required: true,
      host: `127.0.0.1:${wh.port}`,
    },
  ]);
  assert.deepEqual(await features(admin), { live: false, final: false, diarization: false });
});

test("PUT med giltig nyckel: kontrolleras, sparas och gäller direkt utan omstart", async () => {
  const res = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: admin, body: { key: `  ${DG_OK} ` } });
  assert.equal(res.status, 200, res.text);
  assert.deepEqual(res.json.check, { result: "ok", detail: "Nyckeln fungerar." });
  assert.equal(res.json.key.source, "ui");
  assert.equal(res.json.key.set, true);
  assert.equal(res.json.key.hint, "Qx7Z");
  assert.match(res.json.key.updatedBy, /^prov\d+@example\.com$/);
  assert.ok(res.json.key.updatedAt);
  const call = dg.seen.at(-1)!;
  assert.deepEqual([call.method, call.path, call.authorization], ["GET", "/v1/projects", `Token ${DG_OK}`]);
  assert.deepEqual(await features(admin), { live: true, final: false, diarization: true });

  const w = await api("/api/admin/keys/whisper", { method: "PUT", cookie: admin, body: { key: WH_OK } });
  assert.equal(w.status, 200, w.text);
  assert.equal(w.json.check.result, "ok");
  assert.equal(wh.seen.at(-1)!.path, `${GROQ_PREFIX}/models`);
  assert.equal(wh.seen.at(-1)!.authorization, `Bearer ${WH_OK}`);
  assert.deepEqual(await features(user), { live: true, final: true, diarization: true });
  assert.ok(!fs.readFileSync(path.join(server.dataDir, "transcribe.db")).includes(Buffer.from(DG_OK)));
});

test("PUT med nekad nyckel: 400 och ingenting sparas", async () => {
  const res = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: admin, body: { key: DG_BAD } });
  assert.equal(res.status, 400);
  assert.deepEqual(res.json, { error: "Deepgram godkände inte nyckeln. Den sparades inte." });
  const w = await api("/api/admin/keys/whisper", { method: "PUT", cookie: admin, body: { key: DG_BAD } });
  assert.deepEqual(w.json, { error: "Groq godkände inte nyckeln. Den sparades inte." });
  const keys = (await api("/api/admin/keys", { cookie: admin })).json.keys;
  assert.equal(keys[0].hint, "Qx7Z");
  assert.equal(keys[1].hint, "Rt5M");
});

test("PUT när tjänsten inte kan svara: sparas med kontrollen unknown", async () => {
  dg.mode = "503";
  try {
    const res = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: admin, body: { key: DG_OK2 } });
    assert.equal(res.status, 200, res.text);
    assert.deepEqual(res.json.check, { result: "unknown", detail: "Deepgram svarade 503." });
    assert.equal(res.json.key.hint, "Wv3K");
  } finally {
    dg.mode = "normal";
  }
});

test("PUT med ogiltigt format: 400 utan att tjänsten anropas", async () => {
  const other = await invitedAccount(server.url, admin, "admin");
  const seenBefore = dg.seen.length;
  const cases: Array<unknown> = [{}, { key: 12345678 }, { key: "kort" }, { key: "nyckel med mellanslag" }, { key: "x".repeat(513) }];
  for (const body of cases) {
    const res = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: other, body });
    assert.equal(res.status, 400, JSON.stringify(body));
    assert.equal(typeof res.json.error, "string");
  }
  // Trasig JSON: felet citerar inte det som skickades.
  const broken = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: other, rawBody: `{"key": "${DG_BAD}` });
  assert.equal(broken.status, 400);
  assert.ok(!broken.text.includes(DG_BAD) && !broken.text.includes("Zz9Y"));
  assert.equal(dg.seen.length, seenBefore);
  assert.equal((await api("/api/admin/keys", { cookie: admin })).json.keys[0].hint, "Wv3K");
});

test("GET visar aldrig nyckeln, bara de sista tecknen", async () => {
  const res = await api("/api/admin/keys", { cookie: admin });
  for (const k of ALL_KEYS) assert.ok(!res.text.includes(k), "nyckeln finns inte i svaret");
  assert.ok(!res.text.includes("prov-"), "ingen del av nyckeln utöver de sista tecknen");
});

test("test-vägen kontrollerar den nyckel som används just nu; borttagning", async () => {
  const res = await api("/api/admin/keys/deepgram/test", { method: "POST", cookie: admin, body: {} });
  assert.equal(res.status, 200);
  assert.deepEqual(res.json, { check: { result: "ok", detail: "Nyckeln fungerar." } });
  assert.equal(dg.seen.at(-1)!.authorization, `Token ${DG_OK2}`);

  const del = await api("/api/admin/keys/whisper", { method: "DELETE", cookie: admin });
  assert.equal(del.status, 200);
  assert.equal(del.json.key.source, "none");
  assert.equal(del.json.key.set, false);
  assert.deepEqual(await features(admin), { live: true, final: false, diarization: true });
  const missing = await api("/api/admin/keys/whisper/test", { method: "POST", cookie: admin, body: {} });
  assert.deepEqual(missing.json, { check: { result: "missing", detail: "Ingen nyckel är inlagd." } });
});

test("okänt namn ger 404", async () => {
  for (const [method, p] of [
    ["PUT", "/api/admin/keys/openai"],
    ["DELETE", "/api/admin/keys/openai"],
    ["POST", "/api/admin/keys/openai/test"],
  ] as const) {
    const res = await api(p, { method, cookie: admin, body: { key: DG_OK } });
    assert.equal(res.status, 404, `${method} ${p}`);
    assert.equal(typeof res.json.error, "string");
  }
});

/** Öppnar en live-anslutning och väntar tills Deepgram-låtsasservern sett en uppkoppling. */
async function liveAuthorization(): Promise<string> {
  const created = await api("/api/transcripts", { method: "POST", cookie: admin, body: { title: "Prov" } });
  assert.equal(created.status, 200, created.text);
  const before = dg.seen.filter((r) => r.upgrade).length;
  const ws = new WebSocket(`ws://127.0.0.1:${server.port}/api/live/${created.json.id}`, {
    headers: { Cookie: admin, Origin: server.url },
  });
  try {
    const deadline = Date.now() + 10_000;
    while (dg.seen.filter((r) => r.upgrade).length === before) {
      if (Date.now() > deadline) throw new Error("Ingen uppkoppling mot Deepgram");
      await new Promise((r) => setTimeout(r, 50));
    }
    return dg.seen.filter((r) => r.upgrade).at(-1)!.authorization;
  } finally {
    ws.close();
  }
}

test("live-anslutningen använder nyckeln som sparades senast", async () => {
  assert.equal(await liveAuthorization(), `Token ${DG_OK2}`);
  const res = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: admin, body: { key: DG_OK } });
  assert.equal(res.status, 200);
  assert.equal(await liveAuthorization(), `Token ${DG_OK}`);
});

test("spärr: högst 10 kontroller och sparningar per minut och administratör", async () => {
  const limited = await invitedAccount(server.url, admin, "admin");
  for (let i = 0; i < 6; i++) {
    const res = await api("/api/admin/keys/deepgram/test", { method: "POST", cookie: limited, body: {} });
    assert.equal(res.status, 200, `försök ${i + 1}`);
  }
  for (let i = 0; i < 4; i++) {
    const res = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: limited, body: { key: "kort" } });
    assert.equal(res.status, 400, `försök ${i + 7}`);
  }
  const blockedTest = await api("/api/admin/keys/deepgram/test", { method: "POST", cookie: limited, body: {} });
  assert.equal(blockedTest.status, 429);
  assert.deepEqual(blockedTest.json, { error: "För många försök. Vänta en minut." });
  const blockedPut = await api("/api/admin/keys/deepgram", { method: "PUT", cookie: limited, body: { key: DG_OK } });
  assert.equal(blockedPut.status, 429);
  // GET och andra administratörer påverkas inte.
  assert.equal((await api("/api/admin/keys", { cookie: limited })).status, 200);
  assert.equal((await api("/api/admin/keys/deepgram/test", { method: "POST", cookie: admin, body: {} })).status, 200);
});

test("ursprungskontrollen gäller även procentkodade sökvägar", async () => {
  const evil = { Origin: "http://angripare.example" };
  // Utan Origin och utan X-Requested-With: utloggningen nekas och sessionen finns kvar.
  const logout = await api("/%61pi/logout", { method: "POST", cookie: admin, noMarker: true });
  assert.equal(logout.status, 403);
  assert.equal((await api("/api/me", { cookie: admin })).status, 200);

  const put = await api("/%61pi/admin/keys/deepgram", {
    method: "PUT",
    cookie: admin,
    noMarker: true,
    headers: evil,
    body: { key: DG_OK2 },
  });
  assert.equal(put.status, 403);
  const del = await api("/%61pi/admin/keys/deepgram", { method: "DELETE", cookie: admin, noMarker: true, headers: evil });
  assert.equal(del.status, 403);
  const post = await api("/%61pi/admin/keys/deepgram/%74est", { method: "POST", cookie: admin, noMarker: true, headers: evil });
  assert.equal(post.status, 403);
  const plainPath = await api("/api/admin/keys/deepgram", { method: "DELETE", cookie: admin, noMarker: true, headers: evil });
  assert.equal(plainPath.status, 403);
  const status = (await api("/api/admin/keys", { cookie: admin })).json.keys[0];
  assert.deepEqual([status.source, status.hint], ["ui", "Qx7Z"], "ingenting ändrades");

  // Den kodade sökvägen når samma rutter, och svaren får ändå Cache-Control: no-store.
  const get = await api("/%61pi/admin/keys", { cookie: admin });
  assert.equal(get.status, 200);
  assert.equal(get.headers["cache-control"], "no-store");
  const me = await api("/%61%70%69/me", { cookie: admin });
  assert.equal(me.status, 200);
  assert.equal(me.headers["cache-control"], "no-store");
  const missing = await api("/%61pi/finns-inte");
  assert.equal(missing.status, 404);
  assert.equal(missing.headers["cache-control"], "no-store");
  assert.equal(typeof missing.json.error, "string");
  // Vanliga sidor påverkas inte.
  const index = await api("/");
  assert.equal(index.status, 200);
  assert.notEqual(index.headers["cache-control"], "no-store");
});

test("nycklarna finns varken i något svar eller i serverns logg", async () => {
  for (const text of allResponses) {
    for (const k of ALL_KEYS) assert.ok(!text.includes(k), `nyckel i svar: ${text.slice(0, 80)}`);
  }
  const log = server.output();
  assert.match(log, /API-nyckel sparad/);
  assert.match(log, /API-nyckel borttagen/);
  assert.match(log, /API-nyckeln nekades/);
  for (const k of ALL_KEYS) assert.ok(!log.includes(k), "nyckel i loggen");
  for (const h of HINTS) assert.ok(!log.includes(h), "nyckelns sista tecken i loggen");
  const saved = log.split("\n").find((l) => l.includes("API-nyckel sparad"))!;
  const entry = JSON.parse(saved) as Record<string, unknown>;
  assert.equal(entry.key, "deepgram");
  assert.equal(typeof entry.userId, "number");
  assert.equal(entry.check, "ok");
});
