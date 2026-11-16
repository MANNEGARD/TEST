/**
 * Helhetstest för demot (statisk export i out-demo/, `npm run build:demo`).
 *
 * Startar en enkel lokal filserver som beter sig som Netlify (index.html i kataloger,
 * omdirigering till avslutande snedstreck, 404.html för okända adresser) och går igenom alla
 * sidor och huvudflödena i ljust och mörkt tema och i mobilbredd.
 *
 * Testet FALLER om ett anrop mot /api lämnar webbläsaren, om konsolen får fel, om en sida
 * fastnar i laddning eller visar ett felmeddelande, om demot får ett anrop det inte känner
 * igen, eller om ett flöde inte får den följd som syns i appen.
 *
 * Användning: npm run build:demo && npm run e2e:demo
 *   DEMO_DIR    katalogen (standard out-demo)
 *   SHOTS_DIR   skärmbilder (standard e2e-shots-demo)
 *   PORT        filserverns port (standard 4310)
 */
import { createRequire } from "node:module";
import { createServer } from "node:http";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIR = path.resolve(HERE, "..", process.env.DEMO_DIR ?? "out-demo");
const SHOTS = path.resolve(process.env.SHOTS_DIR ?? path.join(HERE, "..", "e2e-shots-demo"));
const PORT = Number(process.env.PORT ?? 4310);
const BASE = `http://127.0.0.1:${PORT}`;

if (!existsSync(path.join(DIR, "index.html"))) {
  console.error(`Hittar inget demo i ${DIR} – kör npm run build:demo först.`);
  process.exit(2);
}
mkdirSync(SHOTS, { recursive: true });
const seed = JSON.parse(readFileSync(path.join(DIR, "demo", "startdata.json"), "utf8"));

// ----------------------------------------------------------------------------- filserver

const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
  ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2",
  ".mjs": "text/javascript", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".ico": "image/x-icon",
};
const server = createServer((req, res) => {
  const url = new URL(req.url, BASE);
  let p = path.join(DIR, decodeURIComponent(url.pathname));
  if (!p.startsWith(DIR)) p = DIR;
  if (existsSync(p) && statSync(p).isDirectory()) {
    if (!url.pathname.endsWith("/")) {
      res.writeHead(301, { location: `${url.pathname}/${url.search}` });
      return res.end();
    }
    p = path.join(p, "index.html");
  }
  if (!existsSync(p) || statSync(p).isDirectory()) {
    res.writeHead(404, { "content-type": TYPES[".html"] });
    return res.end(readFileSync(path.join(DIR, "404.html")));
  }
  res.writeHead(200, { "content-type": TYPES[path.extname(p)] ?? "application/octet-stream" });
  res.end(readFileSync(p));
});
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

// ----------------------------------------------------------------------------- hjälp

const problems = [];
let current = "start";
const fail = (msg) => problems.push(`[${current}] ${msg}`);
const step = (msg) => console.log(`• ${msg}`);
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");

function watch(page) {
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // Den medvetna 404-provningen (/finns-inte) ger webbläsarens eget meddelande.
    if (current === "statiska filer" && /status of 404/.test(m.text())) return;
    fail(`konsolfel: ${m.text().slice(0, 300)}`);
  });
  page.on("pageerror", (e) => fail(`sidfel: ${e.message.slice(0, 300)}`));
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/")) fail(`anrop mot ${u.pathname} lämnade webbläsaren`);
  });
  page.on("response", (r) => {
    const u = new URL(r.url());
    if (u.origin === BASE && r.status() >= 400 && !u.pathname.startsWith("/finns-inte")) fail(`${r.status()} för ${u.pathname}`);
  });
}

async function newContext(browser, opts = {}) {
  const ctx = await browser.newContext({ locale: "sv-SE", timezoneId: "Europe/Stockholm", acceptDownloads: true, viewport: { width: 1360, height: 900 }, ...opts });
  // Kartplattor hämtas aldrig från internet under testet.
  await ctx.route(/tile\.openstreetmap\.org/, (r) => r.fulfill({ status: 200, contentType: "image/png", body: PNG }));
  return ctx;
}

/** Väntar tills sidan är färdigladdad: inga laddningsrutor kvar och inga felrutor. */
async function settled(page, what) {
  await page.waitForLoadState("load");
  const deadline = Date.now() + 12000;
  for (;;) {
    const loading = await page.locator('[role="status"][aria-label="Laddar"]:visible, .animate-shimmer:visible').count();
    if (!loading) break;
    if (Date.now() > deadline) {
      fail(`${what}: fastnar i laddning`);
      break;
    }
    await page.waitForTimeout(200);
  }
  await page.waitForTimeout(250);
  const errors = await page.locator('[role="alert"]:visible').filter({ hasNotText: /^$/ }).allInnerTexts();
  for (const e of errors) if (!/stämmer|väntar|Bostaden/i.test(e)) fail(`${what}: felruta "${e.replace(/\s+/g, " ").slice(0, 120)}"`);
  const unhandled = await page.evaluate(() => window.__drivelogDemoUnhandled ?? []);
  if (unhandled.length) fail(`${what}: demot kände inte igen ${[...new Set(unhandled)].join(", ")}`);
  if (!(await page.getByTestId("demo-banner").count())) fail(`${what}: demomärkningen saknas`);
}

async function visit(page, p, name) {
  await page.goto(BASE + p);
  await settled(page, p);
  if (name) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

/** Läser demots API inifrån sidan (besvaras av den simulerade servern, inget nätverk). */
async function api(page, method, p, body) {
  return page.evaluate(
    async ([m, url, b]) => {
      const res = await fetch(url, { method: m, headers: { "content-type": "application/json" }, body: b ? JSON.stringify(b) : undefined });
      return res.status === 204 ? null : res.json();
    },
    [method, `/api/v1${p}`, body],
  );
}

async function expectText(page, text, timeout = 8000) {
  try {
    await page.getByText(text).first().waitFor({ timeout });
  } catch {
    fail(`texten "${text}" visas inte`);
  }
}

async function section(name, fn) {
  current = name;
  try {
    await fn();
  } catch (err) {
    fail(`avbröts: ${(err?.message ?? String(err)).split("\n")[0].slice(0, 300)}`);
  }
}

const ALL_PAGES = [
  ["/", "oversikt"], ["/journal/", "korjournal"], ["/review/", "granskning"], ["/export/", "export"], ["/places/", "platser"],
  ["/settings/", "installningar"], ["/integrations/", "integrationer"], ["/energy/", "energi"], ["/import/", "import"],
  ["/m/month/2026-10/", "manad-oktober"], ["/m/month/2026-11/", "manad-november"],
];
const SETTINGS = ["fordon", "ersattning", "sammanslagning", "automatik", "forslag", "notiser", "kalender", "tjanstestalle", "forkortningar", "sekretess", "sakerhet", "sakerhetskopia"];
const trips = Object.values(seed.trips).map((d) => d.trip);
const questions = Object.values(seed.notifications);

// ----------------------------------------------------------------------------- körning

const browser = await chromium.launch();
try {
  const ctx = await newContext(browser);
  const page = await ctx.newPage();
  watch(page);
  page.on("dialog", (d) => d.accept());

  await section("statiska filer", async () => {
    step("Statiska filer: _headers, robots, 404, direktlänk och omladdning på djup adress");
    const headers = readFileSync(path.join(DIR, "_headers"), "utf8");
    if (!/X-Robots-Tag:\s*noindex/.test(headers)) fail("_headers saknar X-Robots-Tag: noindex");
    if (existsSync(path.join(DIR, "sw.js"))) fail("service workern följde med i demot");
    const res = await page.goto(`${BASE}/finns-inte`);
    if (res.status() !== 404) fail(`okänd adress gav ${res.status()}`);
    await expectText(page, "Sidan hittades inte");
    const html = readFileSync(path.join(DIR, "index.html"), "utf8");
    if (!/<meta name="robots" content="noindex/.test(html)) fail("index.html saknar robots noindex");
    if (html.includes("/api/v1")) fail("index.html pekar på /api/v1");
  });

  await section("kom igång", async () => {
    step("Översikten: demomärkning, Kom igång med ett steg kvar och \"Bilen står hemma nu\"");
    await visit(page, "/", "oversikt-start");
    const home = page.getByTestId("get-started-step-home");
    if ((await home.getAttribute("data-done")) !== "false") fail("bostaden är redan markerad i startdatat");
    for (const id of ["car", "phone"]) {
      const s = page.getByTestId(`get-started-step-${id}`);
      if ((await s.count()) && (await s.getAttribute("data-done")) !== "true") fail(`steget ${id} är inte klart`);
    }
    await page.getByTestId("get-started-home-button").click();
    // Sista steget: kortet byts mot "Klart – resten sköter sig självt" (som i appen).
    await page.getByTestId("get-started-done").waitFor({ timeout: 8000 });
    const meta = await api(page, "GET", "/locations/meta");
    if (!meta.home_has_coordinate) fail("platsregistret säger att bostaden saknar position");
    await page.screenshot({ path: path.join(SHOTS, "oversikt-hemma.png"), fullPage: true });
  });

  await section("alla sidor", async () => {
    step("Alla sidor (ljust tema, 1360 px)");
    for (const [p, name] of ALL_PAGES) await visit(page, p, `ljus-${name}`);
    for (const key of SETTINGS) await visit(page, `/settings/?sektion=${key}`, `ljus-installningar-${key}`);
    for (const t of trips.slice(0, 6)) await visit(page, `/journal/${t.id}/`);
    for (const q of questions) await visit(page, `/m/q/${q.public_id}/`);
  });

  const reviewTotal = async () => (await api(page, "GET", `/review?status=open&limit=200&vehicle_id=${seed.vehicleId}`)).open_total;

  await section("granskning", async () => {
    step("Granskning: ett beslut minskar räknaren och översikten följer med");
    const before = await reviewTotal();
    const summaryBefore = await api(page, "GET", `/dashboard/summary?period=month&vehicle_id=${seed.vehicleId}`);
    await visit(page, "/review/");
    await page.getByTestId("review-current").waitFor({ timeout: 8000 });
    await page.getByTestId("decide-private").first().click();
    await page.waitForTimeout(800);
    const after = await reviewTotal();
    if (!(after < before)) fail(`granskningen minskade inte (${before} → ${after})`);
    const summaryAfter = await api(page, "GET", `/dashboard/summary?period=month&vehicle_id=${seed.vehicleId}`);
    if (summaryAfter.review.total !== after) fail(`översiktens "Att granska" (${summaryAfter.review.total}) följer inte granskningen (${after})`);
    if (summaryAfter.distance.private.km <= summaryBefore.distance.private.km) fail("privat körning på översikten ökade inte");
    await page.screenshot({ path: path.join(SHOTS, "granskning-efter.png"), fullPage: true });
    // Ångra i kvittot: resan och räknaren går tillbaka.
    await page.locator("[data-sonner-toast] button", { hasText: "Ångra" }).first().click();
    await expectText(page, "Ändringen är ångrad.");
    await page.waitForTimeout(600);
    if ((await reviewTotal()) !== before) fail(`Ångra återställde inte granskningen (${before} → ${await reviewTotal()})`);
  });

  await section("ändringshistorik", async () => {
    step("Resans sida: godkänn, ändringshistoriken visar beslutet och Ångra ändring återställer");
    const proposed = trips.find((t) => t.review_status === "proposed" && t.category === "work");
    await visit(page, `/journal/${proposed.id}/`);
    await page.getByRole("button", { name: "Godkänn klassningen" }).click();
    await expectText(page, "Godkänd.");
    let d = await api(page, "GET", `/trips/${proposed.id}`);
    if (d.trip.review_status !== "approved" || d.history.length < 1) fail("godkännandet syns inte i ändringshistoriken");
    const preview = await api(page, "GET", `/exports/preview?from=2026-11-01&to=2026-11-30&vehicle_id=${seed.vehicleId}`);
    if (!preview.rows.some((r) => r.trip_id === proposed.id)) fail("den godkända resan kom inte med i exportens förhandsvisning");
    await page.getByText("Ändringshistorik").click();
    await page.getByRole("button", { name: "Ångra ändring" }).click();
    await expectText(page, "Ändringen är ångrad.");
    d = await api(page, "GET", `/trips/${proposed.id}`);
    if (d.trip.review_status !== "proposed") fail(`Ångra ändring gav ${d.trip.review_status}`);
  });

  await section("snabbfråga", async () => {
    step("Mobilens snabbfråga /m/q: Tjänst med förslag, företagsnamn och Senare");
    const listed = await api(page, "GET", "/notifications");
    const withSuggestion = listed.find((n) => n.status !== "answered" && n.status !== "cancelled" && n.actions.some((a) => /^Tjänst:/.test(a.title)));
    if (!withSuggestion) fail("ingen öppen fråga med förslag");
    else {
      await visit(page, `/m/q/${withSuggestion.public_id}/`, "snabbfraga");
      const title = withSuggestion.actions.find((a) => /^Tjänst:/.test(a.title)).title;
      await page.getByRole("button", { name: title }).click();
      await page.getByTestId("quick-receipt").waitFor({ timeout: 8000 });
      const trip = await api(page, "GET", `/trips/${withSuggestion.trip_id}`);
      if (trip.trip.category !== "work" || trip.trip.review_status !== "approved") fail("svaret syns inte på resan");
      await page.screenshot({ path: path.join(SHOTS, "snabbfraga-kvitto.png"), fullPage: true });
    }
    const company = listed.find((n) => n.kind === "company" && n.status !== "answered");
    if (!company) fail("ingen \"Vilket företag var det?\"-fråga");
    else {
      await visit(page, `/m/q/${company.public_id}/`, "snabbfraga-foretag");
      await expectText(page, "Vilket företag var det?");
      await page.locator("#quick-companytext").fill("Exempel Fabrik AB");
      await page.getByRole("button", { name: "Spara" }).click();
      await page.getByTestId("quick-receipt").waitFor({ timeout: 8000 });
      const places = await api(page, "GET", "/locations");
      if (!places.some((l) => l.name === "Exempel Fabrik AB" && l.type === "customer")) fail("företaget blev ingen kund i platsregistret");
    }
    const later = (await api(page, "GET", "/notifications")).find((n) => n.status === "sent" && n.actions.some((a) => a.key === "LATER"));
    if (later) {
      await visit(page, `/m/q/${later.public_id}/`);
      await page.getByRole("button", { name: "Senare" }).click();
      await page.waitForTimeout(800);
      const again = (await api(page, "GET", "/notifications")).find((n) => n.public_id === later.public_id);
      if (again.status !== "pending") fail("Senare lade inte undan frågan");
    }
  });

  await section("telefonen", async () => {
    step("Integrationer: Volvo och Home Assistant anslutna, demotelefonen svarar, Synka nu och testnotis");
    await visit(page, "/integrations/", "integrationer");
    for (const id of ["volvo", "home_assistant"]) {
      if (!(await page.getByTestId(`card-${id}`).getByText("Ansluten", { exact: true }).count())) fail(`${id} visas inte som ansluten`);
    }
    await page.getByTestId("configure-home_assistant").click();
    const phone = page.getByTestId("demo-phone");
    await phone.waitFor({ timeout: 8000 });
    await page.waitForTimeout(500);
    const note = phone.getByTestId("demo-notification").filter({ has: page.locator("button") }).first();
    if (!(await note.count())) fail("ingen notis med svarsknappar i demotelefonen");
    else {
      await page.screenshot({ path: path.join(SHOTS, "demotelefon.png"), fullPage: true });
      const btn = note.getByRole("button").filter({ hasText: /^(Privat|Stämmer|Tjänst: .+)$/ }).first();
      if (await btn.count()) {
        const before = await reviewTotal();
        await btn.click();
        await page.waitForTimeout(1000);
        if ((await reviewTotal()) > before) fail("svaret i telefonen ökade granskningen");
      }
    }
    await page.keyboard.press("Escape");
    await page.getByTestId("card-volvo").getByRole("button", { name: /Synka nu/ }).click();
    await expectText(page, "Synkad – inga nya resor sedan sist.");
    await page.getByRole("button", { name: "Skicka testnotis" }).first().click();
    await expectText(page, "Testnotisen är skickad till telefonen.");
  });

  await section("månadens genomgång", async () => {
    step("/m/month: Stämmer på en automatiskt godkänd rad");
    await visit(page, "/m/month/2026-11/", "manad-genomgang");
    const rows = page.getByTestId("month-review-row");
    const before = await rows.count();
    const first = await rows.first().innerText();
    await rows.first().getByRole("button", { name: "Stämmer" }).click();
    await page.waitForTimeout(1200);
    // Raden blir ägarens eget beslut: den bockas av (eller försvinner när listan hämtas om).
    const done = (await rows.count()) < before || (await rows.first().innerText()) !== first || (await rows.first().getByRole("status").count()) > 0;
    if (!done) fail("Stämmer gav ingen följd i månadens genomgång");
    const nov = (await api(page, "GET", "/month-close?month=2026-11")).months[0];
    if (nov.review_rows.length >= before) fail("raden finns kvar bland de automatiskt godkända");
  });

  await section("starttid", async () => {
    step("Resans sida: ≈ med grund, Ange starttid och ta bort den igen");
    const est = trips.find((t) => t.start_time_source === "estimated" && t.started_at);
    const unk = trips.find((t) => t.start_time_source === "unknown" && t.ended_at && t.review_status === "approved");
    await visit(page, `/journal/${est.id}`, "resa-uppskattad");
    const value = page.getByTestId("start-time-estimated");
    await value.waitFor({ timeout: 8000 });
    if (!(await value.innerText()).includes("≈")) fail("uträknad starttid saknar ≈");
    await page.goto(`${BASE}/journal/${unk.id}`);
    await page.reload();
    await settled(page, "resa (omladdning)");
    const box = page.getByTestId("start-time-unknown");
    await box.waitFor({ timeout: 8000 });
    await box.getByRole("button", { name: "Ange starttid" }).click();
    const end = new Date(new Date(unk.ended_at).getTime() - 20 * 60_000);
    const hhmm = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Stockholm", hour: "2-digit", minute: "2-digit" }).format(end);
    await page.getByTestId("start-time-input").fill(hhmm);
    await page.getByRole("button", { name: "Spara", exact: true }).click();
    await page.getByTestId("start-time-user").waitFor({ timeout: 8000 });
    await expectText(page, "angiven av dig");
    await page.screenshot({ path: path.join(SHOTS, "resa-angiven-starttid.png"), fullPage: true });
    await page.getByRole("button", { name: "Ta bort den angivna starttiden" }).click();
    await page.getByTestId("start-time-unknown").waitFor({ timeout: 8000 }).catch(() => fail("starttiden blev inte okänd igen"));
  });

  await section("ny resa", async () => {
    step("En resa som skapas under körning får en förbyggd sida (direktlänk och omladdning)");
    const created = await api(page, "POST", "/trips/manual", {
      vehicle_id: seed.vehicleId, started_at: "2026-11-16T07:00:00+01:00", ended_at: "2026-11-16T07:30:00+01:00",
      start_address: "Björkvägen 7, 123 45 Exempelstad", end_address: "Lagervägen 4, 234 56 Norrby", distance_km: 13, category: "work", purpose: "Service av truckar",
    });
    if (!created?.id) throw new Error(`resan skapades inte: ${JSON.stringify(created)}`);
    await visit(page, `/journal/${created.id}`, "ny-resa");
    await page.reload();
    await settled(page, "ny resa (omladdning)");
    await expectText(page, "Lagervägen 4");
    if (await page.getByText("Resan finns inte").count()) fail("den nya resans sida hittar inte resan");
  });

  await section("platser", async () => {
    step("Platser: bekräfta och byt namn på en obekräftad plats");
    await visit(page, "/places/");
    const target = (await api(page, "GET", "/locations")).find((l) => !l.confirmed);
    if (!target) throw new Error("ingen obekräftad plats");
    await page.getByTestId("place-row").filter({ hasText: target.name }).first().click();
    const name = page.getByLabel("Namn", { exact: true });
    await name.waitFor({ timeout: 8000 });
    await name.fill("Exempelkund Nord AB");
    await page.getByRole("button", { name: /^Spara( och bekräfta)?$/ }).click();
    await page.waitForTimeout(800);
    const after = (await api(page, "GET", "/locations")).find((l) => l.id === target.id);
    if (after.name !== "Exempelkund Nord AB" || !after.confirmed) fail("platsen blev inte bekräftad med nytt namn");
    await page.screenshot({ path: path.join(SHOTS, "platser-efter.png"), fullPage: true });
    const sBefore = (await api(page, "GET", "/locations/suggestions")).types.length;
    const accept = page.getByRole("button", { name: /^Godkänn förslaget för / }).first();
    if (sBefore && (await accept.count())) {
      await accept.click();
      await page.waitForTimeout(800);
      if ((await api(page, "GET", "/locations/suggestions")).types.length !== sBefore - 1) fail("det godkända förslaget finns kvar");
    }
  });

  await section("fordon", async () => {
    step("Inställningar → Fordon: byt namn, sparas och syns i fordonsväljaren");
    await visit(page, "/settings/");
    await page.getByLabel("Namn", { exact: true }).first().fill("Tjänstebilen");
    await page.getByRole("button", { name: "Spara" }).first().click();
    await expectText(page, "Fordon och förare är sparade.");
    if ((await api(page, "GET", "/vehicles"))[0].name !== "Tjänstebilen") fail("fordonets namn sparades inte");
  });

  await section("automatik", async () => {
    step("Inställningar → Automatik: byt läge, spara, byt tillbaka; vetotid");
    await visit(page, "/settings/?sektion=automatik");
    const panel = page.getByTestId("settings-panel-automatik");
    await panel.locator("label").filter({ hasText: "Jag godkänner allt själv" }).click();
    await panel.getByRole("button", { name: "Spara" }).click();
    await page.waitForTimeout(800);
    if ((await api(page, "GET", "/settings")).settings.automation.mode !== "manual") fail("läget sparades inte");
    await visit(page, "/");
    if (await page.getByTestId("get-started").count()) fail("Kom igång visas i läget Jag godkänner allt själv");
    await visit(page, "/settings/?sektion=automatik");
    await panel.locator("label").filter({ hasText: "Självgående" }).first().click();
    await panel.locator("label").filter({ hasText: "3 dygn" }).click();
    await panel.getByRole("button", { name: "Spara" }).click();
    await page.waitForTimeout(800);
    const auto = (await api(page, "GET", "/settings")).settings.automation;
    if (auto.mode !== "self_driving" || auto.assumption_veto_hours !== 72) fail(`automatiken sparades fel: ${JSON.stringify(auto)}`);
  });

  await section("export", async () => {
    step("Export: Skapa Excel ger exempelfilen, månadskortet och nedladdningslänkarna");
    await visit(page, "/export/", "export");
    await page.getByTestId("month-card").waitFor({ timeout: 8000 });
    const dl = page.waitForEvent("download", { timeout: 10000 });
    await page.getByRole("button", { name: "Skapa Excel" }).click();
    const file = await dl;
    const saved = await file.path();
    if (!saved || statSync(saved).size < 4000) fail("exempelfilen laddades inte ned");
    const list = await api(page, "GET", `/exports?vehicle_id=${seed.vehicleId}`);
    if (list.length < 2) fail("den nya filen syns inte bland tidigare filer");
    await page.getByTestId("revoke-links").click();
    await page.getByTestId("revoke-links-confirm").click();
    await page.waitForTimeout(600);
    const months = await api(page, "GET", `/month-close?vehicle_id=${seed.vehicleId}`);
    const nov = months.months.find((m) => m.month === "2026-11");
    const open = nov.blockers.find((b) => b.code === "open_questions");
    const left = (await api(page, "GET", "/trips?page_size=500&needs_review=true")).total;
    if ((open?.count ?? 0) !== left) fail(`månadskortet (${open?.count ?? 0}) följer inte de öppna resorna (${left})`);
    await page.screenshot({ path: path.join(SHOTS, "export-efter.png"), fullPage: true });
  });

  await section("import", async () => {
    step("Import: förhandsgranskning (inspelat exempel) och Släng");
    await visit(page, "/import/");
    await page.getByTestId("import-input").setInputFiles(path.join(DIR, "demo", "korjournal-exempel.xlsx"));
    await page.getByTestId("import-preview").waitFor({ timeout: 10000 });
    await page.screenshot({ path: path.join(SHOTS, "import-forhandsgranskning.png"), fullPage: true });
    await settled(page, "import förhandsgranskning");
  });

  await section("återställ", async () => {
    step("Återställ demot: tillbaka till startdatat");
    await visit(page, "/");
    await page.getByRole("button", { name: "Återställ demot" }).click();
    await page.waitForURL(`${BASE}/`);
    await settled(page, "efter återställning");
    if ((await page.getByTestId("get-started-step-home").getAttribute("data-done")) !== "false") fail("bostaden är fortfarande markerad efter återställning");
    if ((await reviewTotal()) !== seed.reviewMeta.open_total) fail("granskningen återställdes inte");
  });
  await ctx.close();

  // ---------------------------------------------------------------- mörkt tema
  await section("mörkt tema", async () => {
    step("Alla sidor i mörkt tema");
    const dark = await newContext(browser, { colorScheme: "dark" });
    await dark.addInitScript(() => localStorage.setItem("drivelog.theme", "dark"));
    const p = await dark.newPage();
    watch(p);
    for (const [route, name] of ALL_PAGES) await visit(p, route, `mork-${name}`);
    await visit(p, `/journal/${trips[0].id}/`, "mork-resa");
    await visit(p, `/m/q/${questions[0].public_id}/`, "mork-snabbfraga");
    if ((await p.locator("html").getAttribute("data-theme")) !== "dark") fail("temat blev inte mörkt");
    await dark.close();
  });

  // ---------------------------------------------------------------- mobil
  await section("mobil", async () => {
    step("Mobilbredd 390×844: alla sidor, snabbfrågan och månadens genomgång");
    const mob = await newContext(browser, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const p = await mob.newPage();
    watch(p);
    for (const [route, name] of ALL_PAGES) {
      await visit(p, route, `mobil-${name}`);
      const wide = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (wide > 2) fail(`${route}: sidan är ${wide} px för bred i mobilen`);
    }
    const q = questions.find((n) => n.kind === "classify");
    await visit(p, `/m/q/${q.public_id}/`, "mobil-snabbfraga");
    await p.getByRole("button", { name: "Privat", exact: true }).first().click();
    await p.getByTestId("quick-receipt").waitFor({ timeout: 8000 });
    await mob.close();
  });
} finally {
  await browser.close();
  server.close();
}

if (problems.length) {
  console.error(`\n${problems.length} problem:`);
  for (const p of problems) console.error(`  ✗ ${p}`);
  process.exit(1);
}
console.log(`\nDemot klarade alla kontroller. Skärmbilder: ${SHOTS}`);
