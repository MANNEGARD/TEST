/**
 * Helhetstest för DriveLogs webbgränssnitt (Playwright/Chromium) över alla sidor.
 *
 * Förutsätter att backend och frontend kör mot en TOM databas, t.ex. via
 * scripts/e2e.sh (som startar allt på egna portar och kör detta skript).
 *
 * Flöde: första kontot (/setup) → import via webbgränssnittet → Översikt → Körjournal →
 * Resedetalj → Granskning → Export (förhandsvisning = API = Excel-fil) → Platser →
 * Inställningar (ersättning per mil slår igenom, Automatik, sekretess och OSM) → Integrationer
 * (demolägen, starttid "≈" och "Ange starttid", kartbildernas Referer, kalenderlänken utan länk och med ogiltig länk) →
 * Bränsle & energi → månadskortet och nedladdningslänkarna → banderollen för Home Assistant →
 * mobilvy 390×844 → mörkt tema →
 * utloggning och inloggning med retur.
 *
 * Miljövariabler:
 *   BASE_URL     standard http://localhost:3000
 *   SAMPLES_DIR  katalog med exempelexporter (scripts/make_sample_exports.py)
 *   SHOTS_DIR    katalog för skärmbilder
 *   PYTHON       Python med openpyxl (standard ../backend/.venv/bin/python)
 *   WORK_DIR     katalog för nedladdade filer (standard SHOTS_DIR)
 *
 * Misslyckas (exitkod 1) vid konsolfel, sidfel, oväntade 4xx/5xx-svar, nätverksfel,
 * saknade nyckeltexter eller avvikande siffror.
 */
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BASE = process.env.BASE_URL ?? "http://localhost:3000";
const SAMPLES = process.env.SAMPLES_DIR;
const SHOTS = process.env.SHOTS_DIR ?? path.resolve("e2e-shots");
const PYTHON = process.env.PYTHON ?? path.resolve(HERE, "../../backend/.venv/bin/python");
const WORK = process.env.WORK_DIR ?? SHOTS;
const EMAIL = "e2e@example.com";
const PASSWORD = "ett-langt-testlosenord";
/** Ord som aldrig ska synas: luckor i mätarställningen är privat körning, inte fel. */
const GAP_WORDS = /\b(lucka|luckan|luckor|luckorna|datalucka|ofullständig\w*)\b/i;

if (!SAMPLES) {
  console.error("Ange SAMPLES_DIR (kör scripts/make_sample_exports.py först).");
  process.exit(2);
}
mkdirSync(SHOTS, { recursive: true });
mkdirSync(WORK, { recursive: true });

const problems = [];
process.on("unhandledRejection", (err) => problems.push(`ohanterat löfte: ${(err?.stack ?? String(err)).split("\n").slice(0, 3).join(" | ")}`));
/** Svar som är förväntade i flödet (t.ex. auth-kontroll innan inloggning). */
const EXPECTED = [
  { status: 401, url: /\/api\/v1\/auth\/me$/ },
  // "Bilen står hemma nu" innan bilen rapporterat någon position (Kom igång på ny databas).
  { status: 400, url: /\/api\/v1\/locations\/home-from-vehicle$/ },
  // Kalenderlänken: ogiltiga länkar avvisas med klartextfel (avsnittet Integrationer).
  { status: 400, url: /\/api\/v1\/integrations\/microsoft\/ics$/ },
  // Granskning: "Godkänn förslag" på ett förslag som hunnit ändras avvisas (avsnittet Granskning).
  { status: 409, url: /\/api\/v1\/trips\/[^/]+\/approve$/ },
];
/** Kartbildsanrop som testet besvarat lokalt: { url, referer }. */
const tileRequests = [];
// 1×1 transparent PNG – kartplattor hämtas aldrig från internet under testet.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");

function step(msg) {
  console.log(`• ${msg}`);
}

function fail(tag, msg) {
  problems.push(`[${tag}] ${msg}`);
}

function watch(page, tag) {
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    if (/Failed to load resource/.test(text)) return; // bevakas via response-händelsen nedan
    problems.push(`[${tag}] konsolfel: ${text.slice(0, 300)}`);
  });
  page.on("pageerror", (e) => problems.push(`[${tag}] sidfel: ${e.message}`));
  page.on("response", (r) => {
    const url = r.url();
    if (!url.startsWith(BASE)) return;
    const s = r.status();
    if (s < 400) return;
    if (EXPECTED.some((e) => e.status === s && e.url.test(url))) return;
    problems.push(`[${tag}] ${s} ${r.request().method()} ${url.replace(BASE, "")}`);
  });
  page.on("requestfailed", (r) => {
    const url = r.url();
    if (!url.startsWith(BASE)) return;
    const err = r.failure()?.errorText ?? "";
    if (/ERR_ABORTED/.test(err)) return; // avbrutna förfrågningar vid navigering
    problems.push(`[${tag}] nätverksfel ${err} ${url.replace(BASE, "")}`);
  });
}

/** Kör ett avsnitt; ett avbrott registreras och testet fortsätter med nästa avsnitt. */
async function section(tag, fn) {
  try {
    await fn();
  } catch (err) {
    problems.push(`[${tag}] avbrott: ${(err?.stack ?? String(err)).split("\n").slice(0, 4).join(" | ")}`);
  }
}

async function newContext(browser, { mobile = false, theme = "light", storageState } = {}) {
  const ctx = await browser.newContext({
    viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 960 },
    deviceScaleFactor: 1,
    colorScheme: theme,
    isMobile: mobile,
    hasTouch: mobile,
    locale: "sv-SE",
    timezoneId: "Europe/Stockholm",
    acceptDownloads: true,
    storageState,
  });
  // Externa förfrågningar (kartplattor) besvaras lokalt. Kartbildernas Referer sparas: OpenStreetMap
  // kräver sidans ursprung, annars "403 Access blocked".
  await ctx.route(/^https?:\/\/(?!localhost|127\.0\.0\.1)/, async (route) => {
    const req = route.request();
    if (/\/\d+\/\d+\/\d+\.png(\?|$)/.test(req.url())) tileRequests.push({ url: req.url(), referer: await req.headerValue("referer") });
    await route.fulfill({ status: 200, contentType: "image/png", body: PNG });
  });
  return ctx;
}

function visible(page, text, opts = {}) {
  return page.getByText(text, { exact: false, ...opts }).filter({ visible: true }).first();
}

async function expectText(page, text, tag, timeout = 15000) {
  try {
    await visible(page, text).waitFor({ state: "visible", timeout });
    return true;
  } catch {
    fail(tag, `saknar texten "${text}"`);
    return false;
  }
}

async function noGapWords(page, tag) {
  const text = await page.locator("body").innerText();
  const m = text.match(GAP_WORDS);
  if (m) fail(tag, `sidan nämner "${m[0]}" (…${text.slice(Math.max(0, m.index - 60), m.index + 60).replace(/\s+/g, " ")}…)`);
}

async function shot(page, name) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(400);
  // Mobil: skärmbild av vyn (fasta menyer hamnar annars mitt i en helsidesbild).
  const mobile = (page.viewportSize()?.width ?? 1440) < 600;
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: !mobile });
}

async function setPeriodYear(page) {
  await page.getByRole("button", { name: /^Period:/ }).click();
  await page.getByRole("option", { name: "Detta år" }).click();
}

/** Svensk talsträng ("1 234,5") → tal. */
function svNum(s) {
  return Number(String(s).replace(/[\s  ]/g, "").replace("−", "-").replace(",", "."));
}

async function api(page, method, url, data) {
  const me = await (await page.request.get(`${BASE}/api/v1/auth/me`)).json();
  const res = await page.request.fetch(`${BASE}/api/v1${url}`, {
    method,
    headers: { "X-CSRF-Token": me.csrf_token, ...(data ? { "Content-Type": "application/json" } : {}) },
    data: data ? JSON.stringify(data) : undefined,
  });
  if (!res.ok()) throw new Error(`${method} ${url} gav ${res.status()}: ${(await res.text()).slice(0, 300)}`);
  return res.status() === 204 ? null : res.json();
}

/** Väntar på ett svar. Ett uteblivet svar blir ett avbrott där det inväntas – aldrig ett ohanterat löfte. */
function responseTo(page, method, re) {
  const p = page.waitForResponse((r) => r.request().method() === method && re.test(r.url().split("?")[0]), { timeout: 20000 });
  p.catch(() => {});
  return p;
}

/** HH:MM i Europe/Stockholm (samma tidszon som webbläsarkontexten och appens standard). */
/** Lägger in en avläsning med påhittad position för ett fordon (argument: fordonets id, rapporterad tid). */
const INSERT_OBSERVATION = `
import sys, uuid
from datetime import UTC, datetime
import app.models as M
from app.db import session_scope
vid, reported = uuid.UUID(sys.argv[1]), datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
now = datetime.now(UTC)
with session_scope() as db:
    db.add(M.VehicleObservation(vehicle_id=vid, source="e2e", fetched_at=now, last_seen_at=now, latitude=59.0,
                                longitude=15.0, position_reported_at=reported, processed_at=now,
                                content_hash=uuid.uuid4().hex))
`;

function stockholmTime(iso) {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Stockholm", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso));
}

/**
 * Starttiden på resans sida: "≈" med grund för en uträknad tid, och "Start okänd" med
 * "Ange starttid" för en okänd (byggkontraktet D.3). Kräver live-resor (Volvo-demon).
 */
async function checkStartTimes(page, tag) {
  const vehicles = await api(page, "GET", "/vehicles");
  const demo = vehicles.find((v) => v.is_demo);
  if (!demo) {
    fail(tag, "inget demofordon för att pröva starttiden");
    return;
  }
  const list = await api(page, "GET", `/trips?limit=100&vehicle_id=${demo.id}`);
  const estimated = list.items.find((t) => t.start_time_source === "estimated" && t.started_at);
  const unknown = list.items.find((t) => t.start_time_source === "unknown" && t.ended_at);
  if (!estimated && !unknown) {
    fail(tag, `ingen live-resa med uträknad eller okänd starttid bland ${list.items.length} demoresor`);
    return;
  }
  if (estimated) {
    await page.goto(`${BASE}/journal/${estimated.id}`);
    const value = page.getByTestId("start-time-estimated");
    await value.waitFor({ timeout: 15000 });
    const text = await value.innerText();
    if (!text.includes("≈")) fail(tag, `uträknad starttid saknar "≈": ${text}`);
    if (estimated.start_estimate_basis?.margin_min != null && !/±\d+ min/.test(text)) fail(tag, `felmarginalen visas inte: ${text}`);
    await value.getByRole("button", { expanded: false }).first().click();
    const basis = page.getByTestId("start-time-basis");
    await basis.waitFor({ timeout: 5000 }).catch(() => fail(tag, "grunden för den uträknade tiden visas inte"));
    await shot(page, "trip-start-estimated-light");
  } else step("  (ingen demoresa med uträknad starttid – bara okänd prövas)");
  // Ägarens egen starttid: på en okänd starttid ("Ange starttid") eller, när alla starttider är
  // uppskattade, på en uppskattad ("Ange starttiden själv"). Tas den bort gäller det som gällde förut.
  const target = unknown ?? estimated;
  await page.goto(`${BASE}/journal/${target.id}`);
  const before = unknown ? "start-time-unknown" : "start-time-estimated";
  const box = page.getByTestId(before);
  await box.waitFor({ timeout: 15000 });
  if (unknown && !(await box.innerText()).includes("Start okänd")) fail(tag, "okänd starttid visas inte som \"Start okänd\"");
  await box.getByRole("button", { name: unknown ? "Ange starttid" : "Ange starttiden själv" }).click();
  const input = page.getByTestId("start-time-input");
  await input.waitFor({ timeout: 5000 });
  const own = unknown ? new Date(new Date(unknown.ended_at).getTime() - 20 * 60_000).toISOString() : estimated.started_at;
  await input.fill(stockholmTime(own));
  const saved = responseTo(page, "PATCH", /\/api\/v1\/trips\/[^/]+$/);
  await page.getByRole("button", { name: "Spara", exact: true }).click();
  if ((await saved).status() !== 200) fail(tag, "den angivna starttiden sparades inte");
  await page.getByTestId("start-time-user").waitFor({ timeout: 10000 });
  await expectText(page, "angiven av dig", tag);
  await shot(page, "trip-start-user-light");
  const removed = responseTo(page, "PATCH", /\/api\/v1\/trips\/[^/]+$/);
  await page.getByRole("button", { name: "Ta bort den angivna starttiden" }).click();
  if ((await removed).status() !== 200) fail(tag, "den angivna starttiden togs inte bort");
  await page
    .getByTestId(before)
    .waitFor({ timeout: 10000 })
    .catch(() => fail(tag, unknown ? "starttiden blev inte okänd igen" : "uppskattningen gäller inte igen efter att den angivna tiden togs bort"));
}

/**
 * Kartan på resans sida: kartbilderna hämtas med sidans ursprung som Referer (bara ursprunget,
 * aldrig sökvägen), trots att sidan i övrigt skickar Referrer-Policy: same-origin.
 */
async function checkTileReferer(page, tag, tripId) {
  const before = tileRequests.length;
  await page.goto(`${BASE}/journal/${tripId}`);
  const deadline = Date.now() + 20000;
  while (tileRequests.length === before && Date.now() < deadline) await page.waitForTimeout(250);
  const seen = tileRequests.slice(before);
  if (!seen.length) {
    fail(tag, "kartan hämtade inga kartbilder");
    return;
  }
  const policy = (await page.request.get(`${BASE}/journal/${tripId}`)).headers()["referrer-policy"];
  if (policy !== "same-origin") fail(tag, `sidans Referrer-Policy är ${policy}, väntade same-origin`);
  const origin = `${new URL(BASE).origin}/`;
  const wrong = seen.filter((r) => r.referer !== origin);
  if (wrong.length) fail(tag, `${wrong.length} av ${seen.length} kartbilder hade Referer ${JSON.stringify(wrong[0].referer)} i stället för ${origin}`);
  else step(`  ${seen.length} kartbilder med Referer ${origin}`);
  await shot(page, "trip-map-light");
}

// Chromium ger nedladdningar med icke-ASCII-filnamn (Körjournal_…) namnet "download" när
// processens teckenkodning inte är UTF-8 (LANG saknas i containrar) – sätt en UTF-8-lokal.
const LANG = /utf-?8/i.test(process.env.LC_ALL || process.env.LANG || "") ? process.env.LC_ALL || process.env.LANG : "C.UTF-8";
const browser = await chromium.launch({ env: { ...process.env, LANG, LC_ALL: LANG } });
let storageState;
let exportMonth = null;
let reviewRate = null;

try {
  // ================================================================== första kontot + import
  {
    const ctx = await newContext(browser);
    const page = await ctx.newPage();
    watch(page, "setup");
    step("Skapar första kontot via /setup");
    await page.goto(`${BASE}/`);
    await page.waitForURL(/\/setup$/, { timeout: 20000 });
    await expectText(page, "Skapa ditt konto", "setup");
    await shot(page, "setup-light");
    await page.fill("#name", "Testförare");
    await page.fill("#email", EMAIL);
    await page.fill("#password", PASSWORD);
    await page.fill("#confirm", PASSWORD);
    await page.getByRole("button", { name: "Skapa konto och fortsätt" }).click();
    await page.waitForURL(`${BASE}/`, { timeout: 20000 });
    await expectText(page, "Översikt", "setup");

    await section("kom igång", async () => {
      step("Kom igång: kortet på ny databas i självgående läge");
      const card = page.getByTestId("get-started");
      await card.waitFor({ timeout: 15000 });
      for (const [id, done] of [["car", "false"], ["home", "false"], ["phone", "false"]]) {
        const got = await page.getByTestId(`get-started-step-${id}`).getAttribute("data-done");
        if (got !== done) fail("kom igång", `steget ${id} har data-done=${got}, väntade ${done}`);
      }
      await expectText(page, "Din hemadress skickas aldrig till karttjänsten", "kom igång");
      await expectText(page, "Markera på kartan i stället", "kom igång");
      const res = responseTo(page, "POST", /\/api\/v1\/locations\/home-from-vehicle$/);
      await page.getByTestId("get-started-home-button").click();
      if ((await res).status() !== 400) fail("kom igång", "knappen gav inte 400 utan position");
      await page.getByTestId("get-started-home-error").waitFor({ timeout: 10000 });
      await expectText(page, "Bilen har inte rapporterat någon position än", "kom igång");
      await shot(page, "get-started-light");
    });

    step("Import via webbgränssnittet");
    const files = readdirSync(SAMPLES).filter((f) => /\.(xlsx|csv)$/i.test(f));
    if (!files.length) throw new Error(`Inga exempelfiler i ${SAMPLES}`);
    await page.goto(`${BASE}/import`);
    await page.getByTestId("import-dropzone").waitFor({ timeout: 15000 });
    await shot(page, "import-light");
    const previewRes = responseTo(page, "POST", /\/api\/v1\/imports\/preview$/);
    await page.getByTestId("import-input").setInputFiles(files.map((f) => path.join(SAMPLES, f)));
    const pr = await previewRes;
    if (pr.status() !== 201) fail("import", `förhandsgranskning gav ${pr.status()}`);
    const batch = await pr.json();
    await page.getByTestId("import-preview").waitFor({ timeout: 20000 });
    await expectText(page, "Nya resor", "import");
    if (!(batch.summary?.rows_new > 0)) fail("import", `förhandsgranskningen saknar nya resor: ${JSON.stringify(batch.summary).slice(0, 200)}`);
    for (const f of files) await expectText(page, f, "import");
    await shot(page, "import-preview-light");
    const commitRes = responseTo(page, "POST", /\/api\/v1\/imports\/[^/]+\/commit$/);
    await page.getByTestId("import-confirm").click();
    const cr = await commitRes;
    if (!cr.ok()) fail("import", `bekräftelse gav ${cr.status()}`);
    await page.getByTestId("import-receipt").waitFor({ timeout: 30000 });
    await noGapWords(page, "import-kvitto");
    await shot(page, "import-receipt-light");
    await page.getByTestId("import-history").waitFor({ timeout: 15000 }).catch(() => fail("import", "importhistoriken visas inte"));

    storageState = await ctx.storageState();
    await ctx.close();
  }

  // ================================================================== desktop, ljust
  {
    const ctx = await newContext(browser, { storageState });
    const page = await ctx.newPage();
    watch(page, "desktop");

    // ---------------------------------------------------------------- Översikt
    await section("översikt", async () => {
      step("Översikt");
      await page.goto(`${BASE}/`);
      await expectText(page, "Tjänstekörning", "översikt");
      await setPeriodYear(page);
      await expectText(page, "Tjänsteresor", "översikt");
      await expectText(page, "Milersättning", "översikt");
      await expectText(page, "Att granska", "översikt");
      await expectText(page, "Vanligaste destinationerna", "översikt");
      await expectText(page, "Tjänstekörning över tid", "översikt");
      await expectText(page, "Veckan i korthet", "översikt");
      await visible(page, "Veckan i korthet").click();
      await page.waitForTimeout(500);
      await noGapWords(page, "översikt");
      await shot(page, "overview-light");
    });

    // ---------------------------------------------------------------- Körjournal
    await section("körjournal", async () => {
      step("Körjournal: snabbfilter, kategori, markering och massåtgärder");
      await page.goto(`${BASE}/journal`);
      await expectText(page, "Körjournal", "körjournal");
      const rows = page.getByTestId("trip-row");
      await rows.first().waitFor({ timeout: 15000 });
      await expectText(page, "Summa för", "körjournal");
      await noGapWords(page, "körjournal");
      await shot(page, "journal-light");

      const quick = page.getByRole("radiogroup", { name: "Snabbfilter" });
      const QUICK = { Arbete: /category=work/, Privat: /category=private/, "Att granska": /needs_review=true/, Alla: null };
      for (const [name, query] of Object.entries(QUICK)) {
        // "Alla" är startläget och kan komma ur cachen utan ny hämtning.
        const listed = query
          ? page
              .waitForResponse((r) => /\/api\/v1\/trips\?/.test(r.url()) && r.request().method() === "GET" && query.test(r.url().split("?")[1]), { timeout: 10000 })
              .catch(() => null)
          : Promise.resolve(true);
        await quick.getByRole("radio", { name }).click();
        if (!(await listed)) fail("körjournal", `snabbfiltret ${name} hämtade inte rätt urval`);
        await page.waitForTimeout(300);
        const checked = await quick.getByRole("radio", { name }).getAttribute("aria-checked");
        if (checked !== "true") fail("körjournal", `snabbfiltret ${name} blev inte valt`);
      }
      await rows.first().waitFor({ timeout: 15000 });

      const firstRow = rows.first();
      const patch = responseTo(page, "PATCH", /\/api\/v1\/trips\/[^/]+$/);
      await firstRow.getByRole("radio", { name: "Privat" }).click();
      const patchRes = await patch;
      if (patchRes.status() !== 200) fail("körjournal", `kategoribyte gav ${patchRes.status()}`);
      await expectText(page, "Kategori: Privat", "körjournal");
      // Tillbaka till arbete (påverkar annars exportens siffror).
      const patch2 = responseTo(page, "PATCH", /\/api\/v1\/trips\/[^/]+$/);
      await firstRow.getByRole("radio", { name: "Arbete" }).click();
      if ((await patch2).status() !== 200) fail("körjournal", "kategoribyte tillbaka misslyckades");

      await rows.nth(0).getByRole("checkbox").click();
      await rows.nth(1).getByRole("checkbox").click();
      const bar = page.getByRole("toolbar", { name: "Åtgärder för markerade resor" });
      await bar.waitFor({ timeout: 5000 });
      await expectText(page, "2 valda", "körjournal");
      for (const name of ["Arbete", "Privat", "Sätt syfte", "Godkänn", "Favorit", "Slå samman", "Exportera valda", "Avmarkera"]) {
        try {
          await bar.getByRole("button", { name }).click({ trial: true, timeout: 3000 });
        } catch {
          fail("körjournal", `massåtgärden "${name}" går inte att klicka`);
        }
      }
      await shot(page, "journal-selected-light");
      await bar.getByRole("button", { name: "Exportera valda" }).click();
      await page.waitForURL(/\/export\?.*selected=1/, { timeout: 10000 });
      await expectText(page, "Urval", "körjournal→export");
      await expectText(page, "Att redovisa", "körjournal→export");
      await shot(page, "export-selection-light");
    });

    // ---------------------------------------------------------------- Resedetalj
    await section("resedetalj", async () => {
      step("Resedetalj: delresor, dela och ångra");
      const list = await api(page, "GET", "/trips?limit=100");
      const merged = list.items.find((t) => t.merge_kind && t.merge_kind !== "none");
      const target = merged ?? list.items[0];
      await page.goto(`${BASE}/journal/${target.id}`);
      await expectText(page, "Klassning", "resedetalj");
      await expectText(page, "Färdväg", "resedetalj");
      await expectText(page, "Underlag och detaljer", "resedetalj");
      await noGapWords(page, "resedetalj");
      await shot(page, "trip-light");
      if (!merged) {
        fail("resedetalj", "ingen sammanslagen resa efter importen – delning kan inte provas");
        return;
      }
      await visible(page, "Delresor och fördelning").click();
      await expectText(page, "delresor", "resedetalj");
      const split = responseTo(page, "POST", /\/split$/);
      await page.getByRole("button", { name: "Dela resan här" }).first().click();
      if ((await split).status() !== 200) fail("resedetalj", "delningen misslyckades");
      await page.waitForURL((u) => !u.toString().includes(merged.id), { timeout: 10000 });
      const undoBtn = page.getByRole("button", { name: "Ångra", exact: true });
      await undoBtn.waitFor({ timeout: 10000 });
      const undo = responseTo(page, "POST", /\/merges\/[^/]+\/undo$/);
      await undoBtn.click();
      if ((await undo).status() !== 200) fail("resedetalj", "ångra delning misslyckades");
      await expectText(page, "Uppdelningen är ångrad", "resedetalj");
      await page.waitForURL(`${BASE}/journal/${merged.id}`, { timeout: 10000 });
      await expectText(page, "Klassning", "resedetalj");
    });

    // ---------------------------------------------------------------- Granskning
    await section("granskning", async () => {
      step("Granskning och sammanslagningsfråga");
      await page.goto(`${BASE}/review`);
      await expectText(page, "Granskning", "granskning");
      await page.getByTestId("review-current").waitFor({ timeout: 15000 });
      await noGapWords(page, "granskning");
      await shot(page, "review-light");
      const yes = page.getByTestId("merge-yes");
      if (!(await yes.count())) {
        fail("granskning", "ingen sammanslagningsfråga att besvara");
        return;
      }
      const answer = responseTo(page, "POST", /\/merge-questions\/.+\/answer$/);
      await yes.click();
      const ansRes = await answer;
      if (ansRes.status() !== 200) fail("granskning", `svar på sammanslagning gav ${ansRes.status()}`);
      await expectText(page, "slogs samman", "granskning");
    });

    await section("granskning i rask takt", async () => {
      step("Granskning: A/P i rask takt utan att vänta på servern – rätt svar på rätt resa");
      // Importerade resor är redan klassade: sex av dem görs oavgjorda och blir ärenden.
      const imported = (await api(page, "GET", "/trips?page_size=40")).items.filter((t) => t.review_status === "imported");
      for (const t of imported.slice(0, 6)) await api(page, "PATCH", `/trips/${t.id}`, { category: "undecided" });
      await page.goto(`${BASE}/review`);
      const card = page.getByTestId("review-current");
      await card.waitFor({ timeout: 15000 });
      const answered = new Map();
      const started = Date.now();
      for (let n = 0; n < 30 && answered.size < 6; n++) {
        if (!(await card.count())) break; // kön är tom
        const item = await card.getAttribute("data-item-id");
        const trip = await card.getAttribute("data-trip-id");
        if (trip && !answered.has(trip) && (await page.getByTestId("decide-work").count())) {
          const key = answered.size % 2 ? "a" : "p";
          answered.set(trip, key === "a" ? "work" : "private");
          await page.keyboard.press(key);
        } else {
          await page.keyboard.press("ArrowRight");
        }
        // Nästa ärende visas direkt (inget väntar på svaret från servern).
        await page.waitForFunction(
          (prev) => document.querySelector('[data-testid="review-current"]')?.getAttribute("data-item-id") !== prev,
          item,
          { timeout: 1500 },
        );
      }
      const elapsed = Date.now() - started;
      if (answered.size < 4) fail("granskning i rask takt", `bara ${answered.size} resor att klassa`);
      // Svaren sparas i bakgrunden: vänta tills alla har kommit fram och kontrollera varje resa.
      const deadline = Date.now() + 20000;
      let wrong = [];
      do {
        await page.waitForTimeout(300);
        wrong = [];
        for (const [id, category] of answered) {
          const t = (await api(page, "GET", `/trips/${id}`)).trip;
          if (t.category !== category || t.review_status !== "approved") wrong.push(`${id}: ${t.category}/${t.review_status} ≠ ${category}`);
        }
      } while (wrong.length && Date.now() < deadline);
      if (wrong.length) fail("granskning i rask takt", `fel svar på resor: ${wrong.join("; ")}`);
      else step(`  ${answered.size} svar på ${elapsed} ms, alla på rätt resa`);
      // Omräkningen efter svaren blir klar och listan säger det.
      const until = Date.now() + 30000;
      while ((await api(page, "GET", "/review")).recompute_pending && Date.now() < until) await page.waitForTimeout(500);
      if ((await api(page, "GET", "/review")).recompute_pending) fail("granskning i rask takt", "omräkningen blev aldrig klar");

      step("Granskning: ett förslag som hunnit ändras godkänns inte – ärendet läggs tillbaka med felet");
      await page.goto(`${BASE}/review`);
      await card.waitFor({ timeout: 15000 });
      let found = false;
      for (let n = 0; n < 30 && !found; n++) {
        if (!(await card.count())) break;
        if (await page.getByTestId("decide-approve").count()) {
          found = true;
          break;
        }
        const item = await card.getAttribute("data-item-id");
        await page.keyboard.press("ArrowRight");
        await page.waitForFunction(
          (prev) => document.querySelector('[data-testid="review-current"]')?.getAttribute("data-item-id") !== prev,
          item,
          { timeout: 1500 },
        ).catch(() => {});
      }
      if (!found) {
        step("  (inget förslag att godkänna i kön – hoppar över)");
        return;
      }
      const item = await card.getAttribute("data-item-id");
      const trip = await card.getAttribute("data-trip-id");
      await api(page, "PATCH", `/trips/${trip}`, { category: "undecided" }); // förslaget ändras under tiden
      const approveRes = responseTo(page, "POST", /\/trips\/[^/]+\/approve$/);
      await page.keyboard.press("g");
      const res = await approveRes;
      if (res.status() !== 409) fail("granskning i rask takt", `godkännandet gav ${res.status()}, väntade 409`);
      await page.getByTestId("review-failed").waitFor({ timeout: 5000 });
      if ((await card.getAttribute("data-item-id")) !== item) fail("granskning i rask takt", "ärendet lades inte tillbaka först");
      await expectText(page, "Förslaget för resan har ändrats", "granskning i rask takt");
      await shot(page, "review-failed-light");
    });

    // ---------------------------------------------------------------- Export
    await section("export", async () => {
      step("Export: förhandsvisning = API = Excel-fil");
      // Månaden med flest tjänsteresor bland importens månader.
      let best = null;
      for (const m of ["2026-05", "2026-06", "2026-07", "2026-08"]) {
        const last = new Date(Date.UTC(+m.slice(0, 4), +m.slice(5), 0)).getUTCDate();
        const p = await api(page, "GET", `/exports/preview?from=${m}-01&to=${m}-${last}`);
        if (!best || p.rows.length > best.p.rows.length) best = { m, last, p };
      }
      if (!best?.p.rows.length) throw new Error("ingen månad med godkända tjänsteresor");
      exportMonth = best.m;
      const range = `from=${best.m}-01&to=${best.m}-${best.last}`;
      const pagePreview = page.waitForResponse((r) => r.url().includes("/api/v1/exports/preview?") && r.url().includes(`from=${best.m}-01`), { timeout: 20000 });
      await page.goto(`${BASE}/export?manad=${best.m}`);
      const uiPreview = await (await pagePreview).json();
      await expectText(page, "Att redovisa", "export");
      await page.locator("table tbody tr").first().waitFor({ timeout: 15000 });
      await page.waitForTimeout(300);
      const apiPreview = await api(page, "GET", `/exports/preview?${range}&vehicle_id=${uiPreview.vehicle_id}`);
      const uiRows = await page.locator("table tbody tr").count();
      if (uiRows !== apiPreview.rows.length) fail("export", `förhandsvisningen har ${uiRows} rader, API:t ${apiPreview.rows.length}`);
      if (uiPreview.rows.length !== apiPreview.rows.length) fail("export", "sidans förhandsvisning och API:t skiljer sig");
      const sumText = await visible(page, /^Summa, \d+ res/).locator("xpath=following-sibling::dd[1]").innerText();
      const uiKm = svNum(sumText.replace(/km/, ""));
      if (Math.abs(uiKm - apiPreview.totals.km) > 0.05) fail("export", `summan ${uiKm} km ≠ API ${apiPreview.totals.km} km`);
      const sumLabel = await visible(page, /^Summa, \d+ res/).innerText();
      if (svNum(sumLabel.match(/(\d+)/)[1]) !== apiPreview.totals.trips) fail("export", `"${sumLabel}" ≠ ${apiPreview.totals.trips} resor`);
      reviewRate = apiPreview.totals.rate_per_mil;
      await noGapWords(page, "export");
      await shot(page, "export-light");

      const created = responseTo(page, "POST", /\/api\/v1\/exports$/);
      const download = page.waitForEvent("download", { timeout: 30000 });
      download.catch(() => {});
      await page.getByRole("button", { name: "Skapa Excel" }).click();
      const job = await (await created).json();
      const dl = await download;
      const name = dl.suggestedFilename();
      const dlError = await dl.failure();
      if (dlError) fail("export", `nedladdningen misslyckades: ${dlError}`);
      if (name !== job.filename || !/\.xlsx$/i.test(name)) fail("export", `nedladdningen heter ${name}, jobbets filnamn är ${job.filename}`);
      await expectText(page, "Excel-filen är skapad", "export");
      const fileRes = await page.request.get(`${BASE}/api/v1/exports/${job.id}/download`);
      if (!fileRes.ok()) throw new Error(`hämtning av filen gav ${fileRes.status()}`);
      const file = path.join(WORK, `export-${job.id}.xlsx`);
      writeFileSync(file, await fileRes.body());
      const check = JSON.parse(
        execFileSync(
          PYTHON,
          [
            "-I",
            "-c",
            `
import json, sys
from openpyxl import load_workbook
wb = load_workbook(sys.argv[1], data_only=True)
ws = wb[wb.sheetnames[0]]
hdr = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Datum")
rows, km, total = 0, 0.0, None
for r in range(hdr + 1, ws.max_row + 1):
    a = ws.cell(r, 1).value
    if a == "Summa":
        total = ws.cell(r, 8).value
        break
    v = ws.cell(r, 8).value
    if isinstance(v, (int, float)):
        rows += 1
        km += v
print(json.dumps({"sheets": wb.sheetnames, "rows": rows, "km": round(km, 2), "total": total}))
`,
            file,
          ],
          { encoding: "utf8" },
        ),
      );
      if (check.sheets.length !== 1 || check.sheets[0] !== "Körjournal") fail("export", `arbetsboken har bladen ${JSON.stringify(check.sheets)}`);
      if (check.rows !== apiPreview.rows.length) fail("export", `Excel har ${check.rows} datarader, förhandsvisningen ${apiPreview.rows.length}`);
      if (Math.abs(check.km - apiPreview.totals.km) > 0.05) fail("export", `Excel-summan ${check.km} km ≠ ${apiPreview.totals.km} km`);
      if (check.total == null || Math.abs(check.total - apiPreview.totals.km) > 0.05) fail("export", `Excels summarad ${check.total} ≠ ${apiPreview.totals.km}`);
      step(`  ${best.m}: ${check.rows} rader, ${check.km} km i "${check.sheets[0]}" (${name})`);
    });

    // ---------------------------------------------------------------- Månadens underlag
    await section("månadskort", async () => {
      step("Export: månadskortet, automatiskt skapat underlag, hämtningar och stängda länkar");
      if (!exportMonth) throw new Error("ingen exportmånad att pröva");
      await page.goto(`${BASE}/export?manad=${exportMonth}`);
      const card = page.getByTestId("month-card");
      await card.waitFor({ timeout: 15000 });
      await expectText(page, "Månadens underlag", "månadskort");
      if ((await card.getByTestId("month-row").count()) < 1) fail("månadskort", "månadskortet saknar månader");
      // Månadsskiftet för exportmånaden (förfluten, så force behövs inte – men skadar inte).
      const vehicleId = (await api(page, "GET", "/vehicles")).find((v) => !v.is_demo)?.id;
      const status = await api(page, "POST", `/month-close/${exportMonth}/run?force=true${vehicleId ? `&vehicle_id=${vehicleId}` : ""}`);
      if (status.state !== "exported") fail("månadskort", `månadsskiftet för ${exportMonth} gav läget ${status.state} (${status.blockers.map((b) => b.code).join(", ")})`);
      const jobs = await api(page, "GET", "/exports");
      const auto = jobs.find((j) => j.origin === "month_close");
      for (const j of jobs) if (typeof j.link_downloads !== "number") fail("månadskort", `exporten ${j.id} saknar link_downloads`);
      await page.reload();
      await page.getByTestId("month-card").waitFor({ timeout: 15000 });
      if (auto) {
        await expectText(page, "Automatiskt skapad", "månadskort");
        if (auto.link_downloads > 0) await expectText(page, "via länk", "månadskort");
      } else fail("månadskort", "inget automatiskt skapat underlag efter månadsskiftet");
      await shot(page, "export-month-card-light");

      await page.getByTestId("revoke-links").click();
      const revoked = responseTo(page, "POST", /\/api\/v1\/month-close\/links\/revoke$/);
      await page.getByTestId("revoke-links-confirm").click();
      if ((await revoked).status() !== 204) fail("månadskort", "länkarna stängdes inte");
      await expectText(page, "Alla nedladdningslänkar är stängda", "månadskort");
    });

    // ---------------------------------------------------------------- Platser
    await section("platser", async () => {
      step("Platser: förslag, byt namn, AI-minne");
      await page.goto(`${BASE}/places`);
      await page.getByTestId("place-list").waitFor({ timeout: 15000 });
      await shot(page, "places-light");
      const accept = page.getByTestId("suggestion-accept").first();
      if (await accept.count()) {
        const r = responseTo(page, "POST", /\/locations\/.+/);
        await accept.click();
        const res = await r;
        if (!res.ok()) fail("platser", `godkänt förslag gav ${res.status()}`);
        await page.waitForTimeout(500);
      } else {
        fail("platser", "inga platsförslag att godkänna efter importen");
      }
      const row = page.getByTestId("place-row").first();
      await row.click();
      const nameInput = page.locator("#place-name");
      await nameInput.waitFor({ timeout: 10000 });
      await nameInput.fill("E2E Testplatsen");
      const save = responseTo(page, "PATCH", /\/locations\/[^/]+$/);
      await page.getByRole("button", { name: /^Spara( och bekräfta)?$/ }).click();
      if (!(await save).ok()) fail("platser", "namnbytet sparades inte");
      await page.keyboard.press("Escape");
      await expectText(page, "E2E Testplatsen", "platser");

      await page.getByRole("tab", { name: /AI-minne/ }).click();
      await page.getByTestId("patterns-card").waitFor({ timeout: 10000 });
      await page.getByTestId("rules-card").waitFor({ timeout: 10000 });
      await page.getByTestId("new-rule").click();
      await page.locator("#rule-destination").click();
      await page.getByRole("option").first().click();
      await page.getByRole("radio", { name: "Fråga mig alltid" }).click();
      const post = responseTo(page, "POST", /\/learning\/rules$/);
      await page.getByRole("button", { name: "Spara regel" }).click();
      if ((await post).status() !== 201) fail("platser", "regeln Fråga mig alltid sparades inte");
      const ruleRow = page.getByTestId("rule-row").filter({ hasText: "Fråga mig alltid" }).first();
      await ruleRow.waitFor({ timeout: 10000 });
      await shot(page, "places-memory-light");
      const del = responseTo(page, "DELETE", /\/learning\/patterns\/.+\/rule$/);
      await ruleRow.getByRole("button", { name: /Ta bort regeln/ }).click();
      if ((await del).status() !== 204) fail("platser", "regeln togs inte bort");
      await ruleRow.waitFor({ state: "detached", timeout: 10000 });
    });

    // ---------------------------------------------------------------- Inställningar
    await section("inställningar", async () => {
      step("Inställningar: ersättning per mil 30 kr slår igenom, alla sektioner");
      await page.goto(`${BASE}/settings?sektion=ersattning`);
      const rate = page.locator("#exp-rate");
      await rate.waitFor({ timeout: 15000 });
      const original = await rate.inputValue();
      await rate.fill("30");
      const saveRes = responseTo(page, "PATCH", /\/api\/v1\/settings\/[a-z_]+$/);
      await page.getByTestId("settings-panel-ersattning").getByRole("button", { name: "Spara" }).click();
      if (!(await saveRes).ok()) fail("inställningar", "ersättningen sparades inte");
      await expectText(page, "Milersättning och export är sparade", "inställningar");

      await page.goto(`${BASE}/`);
      await setPeriodYear(page);
      await expectText(page, "Milersättning", "inställningar→översikt");
      await expectText(page, "× 30 kr/mil", "inställningar→översikt");
      if (exportMonth) {
        await page.goto(`${BASE}/export?manad=${exportMonth}`);
        await expectText(page, "Ersättning à 30", "inställningar→export");
        const p = await api(page, "GET", `/exports/preview?from=${exportMonth}-01&to=${exportMonth}-28`);
        if (p.totals.rate_per_mil !== 30) fail("inställningar", `exporten räknar med ${p.totals.rate_per_mil} kr/mil`);
        if (p.totals.amount != null && Math.abs(p.totals.amount - Math.round(p.totals.mil * 30 * 100) / 100) > 0.01) fail("inställningar", `beloppet ${p.totals.amount} ≠ mil × 30`);
      }

      await page.goto(`${BASE}/settings?sektion=ersattning`);
      await page.locator("#exp-rate").waitFor({ timeout: 15000 });
      await page.locator("#exp-rate").fill(original);
      await page.getByTestId("settings-panel-ersattning").getByRole("button", { name: "Spara" }).click();
      await expectText(page, "Milersättning och export är sparade", "inställningar");
      const after = await api(page, "GET", "/settings");
      const restored = after.settings?.export?.rate_per_mil;
      if (reviewRate != null && restored !== reviewRate) fail("inställningar", `ersättningen återställdes till ${restored}, inte ${reviewRate}`);

      const tabs = page.locator('[data-testid^="settings-tab-"]');
      const keys = await tabs.evaluateAll((els) => els.map((e) => e.getAttribute("data-testid").replace("settings-tab-", "")));
      if (keys.length < 8) fail("inställningar", `bara ${keys.length} sektioner i menyn`);
      for (const key of keys) {
        await page.getByTestId(`settings-tab-${key}`).click();
        const panel = page.getByTestId(`settings-panel-${key}`);
        await panel.waitFor({ state: "visible", timeout: 10000 });
        await page.waitForLoadState("networkidle").catch(() => {});
        if (!(await panel.locator("h2, h3").count())) fail("inställningar", `sektionen ${key} saknar rubrik`);
      }
      await page.getByTestId("settings-tab-ersattning").click();
      await shot(page, "settings-light");
    });

    // ---------------------------------------------------------------- Automatik och sekretess
    await section("automatik", async () => {
      step("Inställningar → Automatik: ett val, kontorstid, veto och skälen i klartext");
      await page.goto(`${BASE}/settings?sektion=automatik`);
      const panel = page.getByTestId("settings-panel-automatik");
      await panel.waitFor({ state: "visible", timeout: 15000 });
      for (const t of [
        "Automatik",
        "Självgående",
        "Jag godkänner allt själv",
        "Kontorstid",
        "Vetotid",
        "Fråga mig först",
        "1 dygn",
        "3 dygn",
        "Din regel för",
        "Som de 3 senaste gångerna",
        "Hemresa efter",
        "Privat plats",
        "Antaget kundbesök",
        "Se och ändra dina egna regler",
      ]) {
        await expectText(page, t, "automatik");
      }
      const modes = panel.locator('input[name="automation-mode"]');
      if ((await modes.count()) !== 2) fail("automatik", `läget har ${await modes.count()} val, inte ETT val med två alternativ`);
      const before = (await api(page, "GET", "/settings")).settings.automation;
      if (!before) throw new Error("GET /settings saknar sektionen automation");
      await panel.locator("label").filter({ hasText: "3 dygn" }).click();
      const sat = await panel.getByRole("button", { name: "lördag" }).getAttribute("aria-pressed");
      await panel.getByRole("button", { name: "lördag" }).click();
      const saved = responseTo(page, "PATCH", /\/api\/v1\/settings\/automation$/);
      await panel.getByRole("button", { name: "Spara" }).click();
      if (!(await saved).ok()) fail("automatik", "automatiken sparades inte");
      await expectText(page, "Automatiken är sparad", "automatik");
      const after = (await api(page, "GET", "/settings")).settings.automation;
      if (after.assumption_veto_hours !== 72) fail("automatik", `vetotiden blev ${after.assumption_veto_hours}, inte 72`);
      if (after.office_hours.days.includes(5) === (sat === "true")) fail("automatik", "lördagen ändrades inte i kontorstiden");
      if (after.active_since !== before.active_since && before.active_since) fail("automatik", "active_since ändrades av att spara");
      await shot(page, "settings-automation-light");
      await api(page, "PATCH", "/settings/automation", { assumption_veto_hours: before.assumption_veto_hours, office_hours: before.office_hours });

      step("Inställningar → Sekretess: OpenStreetMap och erkännandet");
      await page.goto(`${BASE}/settings?sektion=sekretess`);
      await page.getByTestId("settings-panel-sekretess").waitFor({ state: "visible", timeout: 15000 });
      const geo = (await api(page, "GET", "/settings")).settings.privacy.geocoding_enabled;
      await expectText(page, "OpenStreetMap ser serverns adress", "sekretess");
      if (geo) {
        await expectText(page, "OpenStreetMap-bidragsgivare", "sekretess");
        await page.goto(`${BASE}/places`);
        await page.getByTestId("osm-attribution").waitFor({ timeout: 15000 }).catch(() => fail("platser", "erkännandet till OpenStreetMap saknas"));
      } else fail("sekretess", "geokodningen är inte på som standard");
    });

    // ---------------------------------------------------------------- Integrationer
    await section("integrationer", async () => {
      step("Integrationer: status, demolägen, demotelefon");
      await page.goto(`${BASE}/integrations`);
      for (const id of ["volvo", "microsoft", "home_assistant", "ai"]) {
        const card = page.getByTestId(`card-${id}`);
        await card.getByText("Inte konfigurerad").first().waitFor({ timeout: 15000 }).catch(() => fail("integrationer", `kortet ${id} visar inte "Inte konfigurerad"`));
      }
      await shot(page, "integrations-light");
      const sheet = page.getByTestId("integration-sheet");

      await page.getByTestId("configure-volvo").click();
      await sheet.getByTestId("demo-enable").click();
      await sheet.getByTestId("demo-simulate").waitFor({ timeout: 15000 });
      await page.keyboard.press("Escape");
      await sheet.waitFor({ state: "hidden" });
      await page.getByTestId("configure-home_assistant").click();
      await sheet.getByTestId("demo-enable").click();
      await sheet.getByTestId("demo-phone-simulate").waitFor({ timeout: 15000 });
      const question = sheet.locator('[data-testid="demo-notification"][data-kind="classify"]');
      for (let i = 0; i < 6 && !(await question.count()); i++) {
        const sim = responseTo(page, "POST", /\/volvo\/demo\/simulate$/);
        await sheet.getByTestId("demo-phone-simulate").click();
        if (!(await sim).ok()) fail("integrationer", "simuleringen misslyckades");
        await page.waitForTimeout(1500);
      }
      if (!(await question.count())) {
        fail("integrationer", "ingen fråga visades på demotelefonen");
      } else {
        const q = question.first();
        const href = await q.locator("a[href*='/m/q/']").first().getAttribute("href");
        // Mobilens snabbvy: platsetiketterna ska finnas i frågan (inget extra anrop för resan).
        const mq = await ctx.newPage();
        watch(mq, "snabbvy");
        const calls = [];
        mq.on("request", (r) => r.url().includes("/api/v1/trips/") && calls.push(r.url()));
        await mq.goto(new URL(href, BASE).toString());
        // Självgående läge är standard i en ny databas: frågan heter då "Tjänst eller privat?".
        await expectText(mq, "Tjänst eller privat?", "snabbvy");
        await mq.waitForLoadState("networkidle").catch(() => {});
        if (calls.length) fail("snabbvy", `extra anrop för resan: ${calls.join(", ")}`);
        await shot(mq, "quick-question-light");
        await mq.close();

        await shot(page, "demo-phone-light");
        const btn = q.locator("button:not([aria-expanded])").filter({ hasText: /^(Arbete|Privat)$/ }).first();
        const resp = responseTo(page, "POST", /\/notifications\/[^/]+\/respond$/);
        await btn.click();
        if ((await resp).status() !== 200) fail("integrationer", "svaret i demotelefonen gick inte igenom");
      }
      await page.keyboard.press("Escape");
      await sheet.waitFor({ state: "hidden" });
      for (const id of ["volvo", "microsoft", "home_assistant", "ai"]) {
        if (await page.getByTestId(`card-${id}`).getByText("Ansluten", { exact: true }).count()) fail("integrationer", `kortet ${id} visas som Ansluten i demoläget`);
      }
      if (await page.getByTestId("ha-banner").count()) fail("integrationer", "banderollen för Home Assistant visas i demoläget");
      await section("starttid", async () => {
        step("Starttid: \"≈\" med grund och \"Ange starttid\" på demoresor");
        await checkStartTimes(page, "starttid");
      });
      await section("karta", async () => {
        step("Kartan: kartbilderna bär sidans ursprung som Referer (OpenStreetMaps regler)");
        const demo = (await api(page, "GET", "/vehicles")).find((v) => v.is_demo);
        const list = demo ? await api(page, "GET", `/trips?limit=50&vehicle_id=${demo.id}`) : { items: [] };
        const trip = list.items.find((t) => t.ended_at);
        if (!trip) fail("karta", "ingen demoresa med position att visa på kartan");
        else await checkTileReferer(page, "karta", trip.id);
      });
      await page.goto(`${BASE}/integrations`);
      await page.getByTestId("card-volvo").waitFor({ timeout: 15000 });
      // Stäng av demolägena igen.
      for (const id of ["home_assistant", "volvo"]) {
        await page.getByTestId(`configure-${id}`).click();
        await sheet.getByTestId("demo-disable").click();
        await sheet.getByTestId("demo-enable").waitFor({ timeout: 15000 });
        await page.keyboard.press("Escape");
        await sheet.waitFor({ state: "hidden" });
      }
      for (const id of ["volvo", "microsoft", "home_assistant", "ai"]) {
        await page
          .getByTestId(`card-${id}`)
          .getByText("Inte konfigurerad")
          .first()
          .waitFor({ timeout: 15000 })
          .catch(() => fail("integrationer", `kortet ${id} är inte "Inte konfigurerad" efter demoläget`));
      }

      step("Integrationer: kalenderlänken (ICS) utan länk; ogiltiga länkar ger klartextfel och sparas inte");
      await page.getByTestId("configure-microsoft").click();
      const ics = sheet.getByTestId("ics-section");
      await ics.waitFor({ timeout: 15000 });
      if (await sheet.getByTestId("ics-status").count()) fail("kalenderlänk", "status för en sparad länk visas utan länk");
      const icsInput = sheet.getByTestId("ics-link-input").locator("input");
      if ((await icsInput.getAttribute("type")) !== "password") fail("kalenderlänk", "fältet är inget lösenordsfält");
      if (await icsInput.inputValue()) fail("kalenderlänk", "fältet är förifyllt");
      if (!(await sheet.getByTestId("ics-save").isDisabled())) fail("kalenderlänk", "Spara är aktiv med tomt fält");
      await shot(page, "kalenderlank-light");
      // Inga anrop lämnar datorn: båda avvisas innan något hämtas.
      for (const [link, msg] of [
        ["http://calendar.example.com/owa/calendar/kal.ics", "Länken måste börja med https:// eller webcal://."],
        ["https://127.0.0.1/owa/calendar/kal.ics", "Länken pekar på en lokal eller reserverad adress."],
      ]) {
        await icsInput.fill(link);
        const put = responseTo(page, "PUT", /\/integrations\/microsoft\/ics$/);
        await sheet.getByTestId("ics-save").click();
        const resp = await put;
        if (resp.status() !== 400) fail("kalenderlänk", `ogiltig länk gav ${resp.status()}, väntat 400`);
        if ((await resp.text()).includes(link)) fail("kalenderlänk", "felsvaret innehåller länken");
        await expectText(page, msg, "kalenderlänk");
      }
      const icsStatus = await api(page, "GET", "/integrations/microsoft/ics");
      if (icsStatus.has_link || icsStatus.host || "url" in icsStatus) fail("kalenderlänk", `en ogiltig länk sparades: ${JSON.stringify(icsStatus)}`);
      await page.keyboard.press("Escape");
      await sheet.waitFor({ state: "hidden" });
    });

    // ---------------------------------------------------------------- Bränsle & energi
    await section("energi", async () => {
      step("Bränsle & energi: lägg till och ta bort en tankning");
      await page.goto(`${BASE}/energy`);
      const sectionEl = page.getByTestId("fuel-section");
      await sectionEl.waitFor({ timeout: 15000 });
      await page.waitForLoadState("networkidle").catch(() => {});
      const before = await sectionEl.getByTestId("fuel-row").count();
      await sectionEl.getByRole("button", { name: "Lägg till tankning" }).click();
      await page.locator("#fuel-liters").fill("42,5");
      await page.locator("#fuel-cost").fill("825,50");
      const created = responseTo(page, "POST", /\/energy\/fuel$/);
      await page.getByRole("button", { name: "Spara tankning" }).click();
      if (!(await created).ok()) fail("energi", "tankningen sparades inte");
      await sectionEl.getByTestId("fuel-row").nth(before).waitFor({ timeout: 10000 });
      await shot(page, "energy-light");
      const removed = responseTo(page, "DELETE", /\/energy\/fuel\/[^/]+$/);
      await sectionEl.getByRole("button", { name: /^Ta bort tankningen/ }).first().click();
      if (!(await removed).ok()) fail("energi", "tankningen togs inte bort");
      await page.waitForTimeout(500);
      const after = await sectionEl.getByTestId("fuel-row").count();
      if (after !== before) fail("energi", `${after} tankningar efter borttagning, väntade ${before}`);
    });

    // ---------------------------------------------------------------- Global sökning
    await section("sök", async () => {
      step("Global sökning (Ctrl+K)");
      await page.goto(`${BASE}/`);
      await expectText(page, "Tjänstekörning", "sök");
      await page.keyboard.press("Control+k");
      await page.getByRole("textbox", { name: "Sökterm" }).fill("Storstad");
      await page.keyboard.press("Enter");
      await page.waitForURL(/\/journal\?q=Storstad/);
      await page.getByTestId("trip-row").first().waitFor({ timeout: 15000 });
    });

    // ---------------------------------------------------------------- Banderoll: Home Assistant utan telefon
    await section("banderoll", async () => {
      step("Banderoll: Home Assistant i pull-läge utan vald telefon");
      if (await page.getByTestId("ha-banner").count()) fail("banderoll", "banderollen visas innan Home Assistant är kopplat");
      const mode = (await api(page, "GET", "/settings")).settings.notifications?.delivery_mode ?? "push";
      await api(page, "PUT", "/integrations/home-assistant", { delivery_mode: "pull" });
      // Integrationens livstecken med två telefoner och inget val: DriveLog kan inte välja själv.
      const token = await api(page, "POST", "/auth/tokens", { name: "E2E Home Assistant", scopes: ["ha"] });
      const hb = await page.request.fetch(`${BASE}/api/v1/ha/heartbeat`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token.token}`, "Content-Type": "application/json" },
        data: JSON.stringify({ ha_version: "2026.11.0", integration_version: "1.1.0", notify_services: ["notify.mobile_app_telefon_a", "notify.mobile_app_telefon_b"] }),
      });
      if (!hb.ok()) fail("banderoll", `livstecknet gav ${hb.status()}`);
      const status = await api(page, "GET", "/integrations/home-assistant");
      if (status.phone_missing !== true) fail("banderoll", `phone_missing är ${status.phone_missing} utan vald telefon`);
      await page.goto(`${BASE}/`);
      const banner = page.getByTestId("ha-banner");
      await banner.waitFor({ timeout: 15000 }).catch(() => fail("banderoll", "banderollen visas inte"));
      await expectText(page, "Ingen telefon vald", "banderoll");
      await shot(page, "banner-phone-missing-light");
      const off = await api(page, "POST", "/integrations/home-assistant/disconnect");
      if (off.phone_missing) fail("banderoll", "phone_missing är kvar efter frånkopplingen");
      await api(page, "PATCH", "/settings/notifications", { delivery_mode: mode });
      await api(page, "DELETE", `/auth/tokens/${token.id}`);
      await page.reload();
      await page.waitForLoadState("networkidle").catch(() => {});
      if (await page.getByTestId("ha-banner").count()) fail("banderoll", "banderollen ligger kvar efter frånkopplingen");
    });

    // ---------------------------------------------------------------- Kom igång: bostaden med bilens position
    await section("kom igång", async () => {
      step("Kom igång: \"Bilen står hemma nu\" med en inlagd avläsning");
      const db = path.join(WORK, "data.sqlite3");
      if (!existsSync(db)) {
        step(`  (ingen databas i ${db} – avläsningen läggs inte in, steget prövas inte)`);
        return;
      }
      const vehicle = (await api(page, "GET", "/vehicles")).find((v) => !v.is_demo);
      if (!vehicle) throw new Error("inget riktigt fordon efter importen");
      const reported = new Date(Date.now() - 3 * 60_000).toISOString();
      // Påhittad position; avläsningen märks som bearbetad så att resedetekteringen inte rör den.
      execFileSync(PYTHON, ["-c", INSERT_OBSERVATION, vehicle.id, reported], {
        cwd: path.resolve(HERE, "../../backend"),
        env: { ...process.env, DRIVELOG_ENVIRONMENT: "development", DRIVELOG_DATABASE_URL: `sqlite:///${db}`, DRIVELOG_DATA_DIR: path.join(WORK, "data") },
        stdio: ["ignore", "ignore", "inherit"],
      });
      await page.goto(`${BASE}/`);
      await page.getByTestId("get-started").waitFor({ timeout: 15000 });
      const homeStep = page.getByTestId("get-started-step-home");
      await page.getByTestId("get-started-home-address").fill("Påhittade vägen 1, Exempelstad");
      const res = responseTo(page, "POST", /\/api\/v1\/locations\/home-from-vehicle$/);
      await page.getByTestId("get-started-home-button").click();
      const r = await res;
      if (r.status() !== 200) fail("kom igång", `"Bilen står hemma nu" gav ${r.status()}`);
      await page.waitForFunction(() => document.querySelector('[data-testid="get-started-step-home"]')?.getAttribute("data-done") === "true", null, { timeout: 15000 })
        .catch(() => fail("kom igång", "steget för bostaden blev inte klart efter knapptrycket"));
      await expectText(page, `Markerad med bilens position kl. ${stockholmTime(reported)}`, "kom igång");
      const meta = await api(page, "GET", "/locations/meta");
      if (meta.home_has_coordinate !== true) fail("kom igång", "platsöversikten säger att hemmet saknar koordinat");
      if ((await homeStep.getAttribute("data-done")) !== "true") fail("kom igång", "steget för bostaden är inte klart");
      if (!(await page.getByTestId("get-started").count())) fail("kom igång", "kortet försvann fast bilen och telefonen inte är anslutna");
      await shot(page, "get-started-home-done-light");
    });

    storageState = await ctx.storageState();
    await ctx.close();
  }

  // ================================================================== alla sidor: mörkt, mobil
  const PAGES = [
    ["overview", "/", "Tjänstekörning"],
    ["journal", "/journal", "Körjournal"],
    ["review", "/review", "Granskning"],
    ["export", () => `/export?manad=${exportMonth ?? "2026-07"}`, "Att redovisa"],
    ["places", "/places", "Platser"],
    ["settings", "/settings", "Inställningar"],
    ["integrations", "/integrations", "Integrationer"],
    ["import", "/import", "Import"],
    ["energy", "/energy", "Bränsle"],
  ];
  for (const variant of [
    { mobile: false, theme: "dark", suffix: "dark" },
    { mobile: true, theme: "light", suffix: "mobile-light" },
    { mobile: true, theme: "dark", suffix: "mobile-dark" },
  ]) {
    const ctx = await newContext(browser, { ...variant, storageState });
    const page = await ctx.newPage();
    watch(page, variant.suffix);
    step(`Alla sidor (${variant.suffix})`);
    await section(variant.suffix, async () => {
      const list = await api(page, "GET", "/trips?limit=1");
      const pages = [...PAGES, ["trip", `/journal/${list.items[0].id}`, "Klassning"]];
      for (const [name, href, text] of pages) {
        await page.goto(`${BASE}${typeof href === "function" ? href() : href}`);
        await expectText(page, text, `${variant.suffix}/${name}`);
        if (name === "overview") {
          await setPeriodYear(page);
          await expectText(page, "Vanligaste destinationerna", `${variant.suffix}/${name}`);
        }
        if (name === "journal") await page.getByTestId(variant.mobile ? "trip-card" : "trip-row").first().waitFor({ timeout: 15000 });
        await page.waitForLoadState("networkidle").catch(() => {});
        const theme = await page.evaluate(() => document.documentElement.dataset.theme ?? (document.documentElement.classList.contains("dark") ? "dark" : "light"));
        if (variant.theme === "dark" && theme !== "dark") fail(variant.suffix, `${name}: temat är ${theme}, inte mörkt`);
        if (variant.mobile) {
          const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
          if (overflow > 0) fail(variant.suffix, `${name}: horisontell rullning (${overflow}px för bred)`);
        }
        await shot(page, `${name}-${variant.suffix}`);
      }
      if (variant.mobile) {
        await page.getByRole("button", { name: "Mer" }).click();
        await expectText(page, "Inställningar", variant.suffix);
        await shot(page, `menu-${variant.suffix}`);
      }
    });
    await ctx.close();
  }

  // ================================================================== tema, utloggning, inloggning
  {
    const ctx = await newContext(browser, { storageState });
    const page = await ctx.newPage();
    watch(page, "konto");
    await section("tema", async () => {
      step("Växlar tema via menyn");
      await page.goto(`${BASE}/`);
      await expectText(page, "Tjänstekörning", "tema");
      await page.getByTestId("theme-toggle").click();
      await page.getByRole("menuitemradio", { name: "Mörkt" }).click();
      const theme = await page.evaluate(() => document.documentElement.dataset.theme);
      if (theme !== "dark") fail("tema", `förväntade data-theme=dark, fick ${theme}`);
      await page.getByTestId("theme-toggle").click();
      await page.getByRole("menuitemradio", { name: "Ljust" }).click();
    });
    await section("utloggning", async () => {
      step("Loggar ut och in med retur till /export?manad=…");
      await page.getByRole("button", { name: "Användarmeny" }).click();
      await page.getByRole("menuitem", { name: "Logga ut" }).click();
      await page.waitForURL(/\/login/, { timeout: 15000 });
      const month = exportMonth ?? "2026-07";
      await page.goto(`${BASE}/export?manad=${month}`);
      await page.waitForURL(/\/login\?next=/, { timeout: 15000 });
      await expectText(page, "Logga in", "inloggning");
      await shot(page, "login-light");
      await page.fill("#email", EMAIL);
      await page.fill("#password", "fel-losenord-123");
      const failRes = responseTo(page, "POST", /\/api\/v1\/auth\/login$/);
      await page.getByRole("button", { name: "Logga in" }).click();
      const res = await failRes;
      if (res.status() === 401) {
        // Förväntat fel – ta bort det ur listan och kontrollera att texten visas.
        const idx = problems.findIndex((p) => p.includes("401 POST /api/v1/auth/login"));
        if (idx >= 0) problems.splice(idx, 1);
        await expectText(page, "Fel e-postadress eller lösenord", "inloggning");
      } else fail("inloggning", `fel lösenord gav ${res.status()}`);
      await page.fill("#password", PASSWORD);
      await page.getByRole("button", { name: "Logga in" }).click();
      await page.waitForURL(`${BASE}/export?manad=${month}`, { timeout: 15000 });
      await expectText(page, "Att redovisa", "inloggning");
    });
    await ctx.close();
  }

  // ================================================================== inloggningssidan, mörk
  {
    const ctx = await newContext(browser, { theme: "dark" });
    const page = await ctx.newPage();
    watch(page, "login");
    await page.goto(`${BASE}/login`);
    await expectText(page, "Logga in", "login");
    await shot(page, "login-dark");
    await ctx.close();
  }
} catch (err) {
  problems.push(`Avbrott: ${err?.stack ?? err}`);
} finally {
  await browser.close();
}

if (problems.length) {
  console.error(`\n✗ Helhetstestet misslyckades (${problems.length} problem):`);
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`\n✓ Helhetstestet gick igenom. Skärmbilder: ${SHOTS}`);
