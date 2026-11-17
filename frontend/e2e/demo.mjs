/**
 * Helhetstest för demot (statisk export i out-demo/, `npm run build:demo`).
 *
 * Startar en enkel lokal filserver som beter sig som Netlify (index.html i kataloger,
 * omdirigering till avslutande snedstreck, 404.html för okända adresser) och går igenom alla
 * sidor och huvudflödena i ljust och mörkt tema och i mobilbredd.
 *
 * Testet FALLER om ett anrop mot /api lämnar webbläsaren, om konsolen får fel, om en sida
 * fastnar i laddning eller visar ett felmeddelande, om demot får ett anrop det inte känner
 * igen, om Content-Security-Policy (ur _headers) spärrar något, eller om ett flöde inte får den
 * följd som syns i appen.
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
// Rubrikerna för alla sidor ur _headers (avsnittet "/*"), som Netlify skickar dem: bl.a.
// Referrer-Policy och Content-Security-Policy, så att kartbildernas anrop prövas under samma villkor.
const SITE_HEADERS = (() => {
  const out = {};
  const file = path.join(DIR, "_headers");
  if (!existsSync(file)) return out;
  let inAll = false;
  for (const line of readFileSync(file, "utf8").split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    if (!/^\s/.test(line)) {
      inAll = line.trim() === "/*";
      continue;
    }
    const i = line.indexOf(":");
    if (inAll && i > 0) out[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
  }
  return out;
})();
const server = createServer((req, res) => {
  const url = new URL(req.url, BASE);
  for (const [k, v] of Object.entries(SITE_HEADERS)) res.setHeader(k, v);
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
/** Kartbildsanrop som testet besvarat lokalt: { url, referer }. */
const tileRequests = [];
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");

/** URL:er till arbetare som sidorna startat (MapLibres arbetare ska starta på kartsidan). */
const workerUrls = [];
const CSP_TEXT = /Content[- ]Security[- ]Policy/i;

function watch(page) {
  page.on("worker", (w) => workerUrls.push(w.url()));
  page.on("console", (m) => {
    // Överträdelser av Content-Security-Policy (fel eller varning) fäller alltid testet.
    if (CSP_TEXT.test(m.text())) return fail(`Content-Security-Policy: ${m.text().slice(0, 300)}`);
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
  // Kartplattor hämtas aldrig från internet under testet; deras Referer sparas.
  await ctx.route(/tile\.openstreetmap\.org/, async (r) => {
    tileRequests.push({ url: r.request().url(), referer: await r.request().headerValue("referer") });
    await r.fulfill({ status: 200, contentType: "image/png", body: PNG });
  });
  // Content-Security-Policy: webbläsarens securitypolicyviolation-händelser på varje sida, även i
  // sidor som inte bevakas med watch(). En spärrad förfrågan ger en händelse även utan nät.
  await ctx.exposeBinding("__drivelogCspViolation", (_src, v) => {
    fail(`Content-Security-Policy spärrade ${v.directive}: ${v.blocked || "(inline)"} på ${v.href}`);
  });
  await ctx.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      window.__drivelogCspViolation?.({ directive: e.effectiveDirective, blocked: e.blockedURI, href: location.href });
    });
  });
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
const SETTINGS = ["fordon", "ersattning", "sammanslagning", "automatik", "lunch", "forslag", "notiser", "kalender", "tjanstestalle", "forkortningar", "sekretess", "sakerhet", "sakerhetskopia", "om"];
const trips = Object.values(seed.trips).map((d) => d.trip);
const questions = Object.values(seed.notifications);
// Demots importerade resor (1.3.0) har de sista id:na i den förbyggda uppsättningen (state.ts).
const poolTripId = (n) => `00000000-0000-4000-8000-${String(n + 1).padStart(12, "0")}`;
const importedTripId = (n) => poolTripId(40 - 1 - n);
// Demoresan vars ärende kom ur kalendern när den godkändes av ägarens regel (server.ts CALENDAR_TRIP).
const CALENDAR_TRIP = "2e40532a-d7f6-45e3-9e7d-ed7f9a66e8e1";
// Demots avvikelser (1.3.2, server.ts applyV132): en om en resa och en om två resor.
const MISMATCH_TRIP = trips.find((t) => t.id.startsWith("b01e1597"));
const BACKWARDS_TRIPS = ["3fb26e09", "a67ea1f4"].map((p) => trips.find((t) => t.id.startsWith(p)));
/** Antal öppna ärenden när testet börjar (startdatat plus demots avvikelser). */
let startReviewTotal = null;

/** Platsen som granskningen visar för resan: bekräftad plats med namn, annars adressen. */
const placeOf = (t, end) => (t[`${end}_location`]?.confirmed ? t[`${end}_location`].name : t[`${end}_address`]);

/** Visar avvikelsen av typen `type` (eller ärendet `id`) i Granskning (hoppar över ärenden med →).
 *  Returnerar kortet eller null. */
async function showAnomaly(page, type, id) {
  await visit(page, "/review/");
  const card = page.getByTestId("review-current");
  await card.waitFor({ timeout: 8000 });
  const list = await api(page, "GET", `/review?status=open&limit=200&vehicle_id=${seed.vehicleId}`);
  const want = id ?? list.items.find((i) => i.kind === "anomaly" && i.data?.type === type)?.id;
  for (let n = 0; want && n < 20; n++) {
    if ((await card.getAttribute("data-item-id")) === want) return card;
    const prev = await card.getAttribute("data-item-id");
    await page.keyboard.press("ArrowRight");
    await page
      .waitForFunction((id) => document.querySelector('[data-testid="review-current"]')?.getAttribute("data-item-id") !== id, prev, { timeout: 1500 })
      .catch(() => {});
  }
  fail(`avvikelsen ${id ?? type} visas inte i Granskning`);
  return null;
}

/** Mobil: kortet och dess knappar ryms i bredden (inget kapas, inget hamnar utanför). */
async function fitsWidth(page, card, what) {
  const r = await card.evaluate((el) => {
    const box = el.getBoundingClientRect();
    const outside = [...el.querySelectorAll("a, button")]
      .filter((b) => b.getClientRects().length)
      .filter((b) => {
        const bb = b.getBoundingClientRect();
        return bb.left < box.left - 1 || bb.right > box.right + 1;
      })
      .map((b) => b.textContent.trim());
    const clipped = [...el.querySelectorAll("p, dd, dt, span, a, button")]
      // Skärmläsartext (sr-only, 1 px bred) räknas inte.
      .filter((x) => x.getClientRects().length && x.clientWidth > 1 && x.scrollWidth > x.clientWidth + 1 && getComputedStyle(x).overflow !== "visible")
      .map((x) => x.textContent.trim().slice(0, 40));
    return { wide: el.scrollWidth - el.clientWidth, outside, clipped, right: box.right };
  });
  if (r.wide > 1) fail(`${what}: kortet är ${r.wide} px för brett`);
  if (r.outside.length) fail(`${what}: knappar utanför kortet: ${r.outside.join(", ")}`);
  if (r.clipped.length) fail(`${what}: text kapas: ${r.clipped.join(" | ")}`);
  const vw = await page.evaluate(() => window.innerWidth);
  if (r.right > vw + 1) fail(`${what}: kortet går utanför skärmen`);
}

/** Antal rader texten i elementet bryts på (1 = får plats på en rad). */
const lineCount = (locator) =>
  locator.evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const tops = new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top)));
    return tops.size;
  });

// ----------------------------------------------------------------------------- ren logik (säkerhet)

await section("målet efter inloggning", async () => {
  step("safeNext: bara sökvägar på samma ursprung godtas, interna mål med frågesträng som förut");
  const { safeNext } = await import(path.join(HERE, "..", "src", "lib", "safe-next.ts"));
  const origin = "https://drivelog.example";
  for (const bad of [
    "/\\evil.example/x", "/\\\\evil.example", "/\t/evil.example", "/\n/evil.example", "//evil.example",
    "https://evil.example", "/login?next=/x", "/login/", "", null,
  ]) {
    const got = safeNext(bad, origin);
    if (got !== "/") fail(`safeNext(${JSON.stringify(bad)}) gav ${JSON.stringify(got)}`);
  }
  for (const ok of ["/", "/journal/", "/journal/?q=1", "/export/?manad=2026-10", "/journal/abc/#x", "/m/q/abc123/", "/settings/?sektion=om"]) {
    const got = safeNext(ok, origin);
    if (got !== ok) fail(`safeNext(${JSON.stringify(ok)}) gav ${JSON.stringify(got)}`);
  }
});

await section("id i API-sökvägar", async () => {
  step("Id ur adressfältet kodas när det sätts in i en API-sökväg");
  const src = (f) => readFileSync(path.join(HERE, "..", "src", "lib", "queries", f), "utf8");
  for (const [file, raw] of [["trips.ts", "`/trips/${id}`"], ["places.ts", "`/trips/${id}`"], ["notifications.ts", "`/trips/${tripId}`"]]) {
    if (src(file).includes(raw)) fail(`${file}: ${raw} sätts in okodat`);
  }
});

// ----------------------------------------------------------------------------- körning

const browser = await chromium.launch();
try {
  await section("inloggning och next", async () => {
    step("Inloggning: externa mål i ?next= byts mot startsidan, interna mål med frågesträng fungerar");
    const lctx = await newContext(browser);
    // Ett externt mål får aldrig nås; skulle det ändå laddas syns det på adressen.
    await lctx.route((u) => u.hostname === "example.invalid", (r) => r.fulfill({ status: 200, contentType: "text/html", body: "<h1>extern</h1>" }));
    const lp = await lctx.newPage();
    for (const [next, want] of [
      ["/%5Cexample.invalid/x", "/"],
      ["/%09/example.invalid/", "/"],
      [encodeURIComponent("/journal/?q=1"), "/journal/?q=1"],
    ]) {
      await lp.goto(`${BASE}/login/?next=${next}`);
      await lp.getByLabel("E-postadress").fill("demo@example.com");
      await lp.getByLabel("Lösenord", { exact: true }).fill("hemligt-demo");
      await lp.getByRole("button", { name: "Logga in" }).click();
      try {
        await lp.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 8000 });
      } catch {
        /* prövas nedan */
      }
      await lp.waitForTimeout(500);
      const u = new URL(lp.url());
      if (u.origin !== BASE || u.pathname + u.search !== want) fail(`next=${next} ledde till ${lp.url()} (väntat ${want})`);
    }
    await lctx.close();
  });

  await section("återkomst från leverantören", async () => {
    step("Integrationer: kända felkoder och leverantörens kod visas, fri text ur adressen visas inte");
    const octx = await newContext(browser);
    const op = await octx.newPage();
    await op.goto(`${BASE}/integrations/?microsoft=error&reason=unauthorized_client`);
    await expectText(op, "Leverantören svarade: unauthorized_client.");
    await op.goto(`${BASE}/integrations/?volvo=error&reason=access_denied`);
    await expectText(op, "Inloggningen avbröts eller nekades.", 8000);
    const free = "Volvo ID har spärrats. Gå till x.example";
    await op.goto(`${BASE}/integrations/?volvo=error&reason=${encodeURIComponent(free)}`);
    try {
      await op.getByText("Försök igen.", { exact: true }).first().waitFor({ timeout: 8000 });
    } catch {
      fail('texten "Försök igen." visas inte för fri text');
    }
    if (await op.getByText("spärrats", { exact: false }).count()) fail("fri text ur adressfältet visas i appen");
    await octx.close();
  });

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
    startReviewTotal = (await api(page, "GET", `/review?status=open&limit=200&vehicle_id=${seed.vehicleId}`)).open_total;
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

  await section("karta", async () => {
    step("Kartan: kartbilderna bär sidans ursprung som Referer, trots Referrer-Policy: same-origin");
    const withMap = trips.find((t) => t.source === "demo" || t.source === "volvo_api") ?? trips[0];
    const before = tileRequests.length;
    const res = await page.goto(`${BASE}/journal/${withMap.id}/`);
    if (res.headers()["referrer-policy"] !== "same-origin") fail(`sidans Referrer-Policy är ${res.headers()["referrer-policy"]}`);
    if (!/object-src 'none'/.test(res.headers()["content-security-policy"] ?? "")) fail("sidan saknar Content-Security-Policy (demo-static/_headers)");
    const deadline = Date.now() + 15000;
    while (tileRequests.length === before && Date.now() < deadline) await page.waitForTimeout(250);
    const seen = tileRequests.slice(before);
    if (!seen.length) fail("kartan hämtade inga kartbilder");
    const wrong = seen.filter((r) => r.referer !== `${BASE}/`);
    if (wrong.length) fail(`${wrong.length} av ${seen.length} kartbilder hade Referer ${JSON.stringify(wrong[0].referer)} i stället för ${BASE}/`);
    else if (seen.length) step(`  ${seen.length} kartbilder med Referer ${BASE}/`);
    // Kartan ritas under policyn: arbetaren startar och kartytan syns.
    if (!workerUrls.some((u) => u.includes("/vendor/maplibre-gl-worker.mjs"))) fail("MapLibres arbetare startade inte");
    await page.locator("canvas.maplibregl-canvas").first().waitFor({ state: "visible", timeout: 10000 }).catch(() => fail("kartan ritades inte"));
    await page.screenshot({ path: path.join(SHOTS, "karta-resa.png"), fullPage: true });
  });

  const reviewTotal = async () => (await api(page, "GET", `/review?status=open&limit=200&vehicle_id=${seed.vehicleId}`)).open_total;

  await section("AI-genomgång", async () => {
    step("Granskning → Gå igenom väntande med AI (utan leverantör): bara reglerna, förlopp och Klart");
    await visit(page, "/review/");
    await page.getByTestId("review-ai-review").click();
    const dialog = page.getByTestId("ai-review-dialog");
    await dialog.waitFor({ timeout: 5000 });
    await dialog.getByTestId("ai-review-intro").or(dialog.getByTestId("ai-review-empty")).waitFor({ timeout: 8000 });
    if (await dialog.getByTestId("ai-review-empty").count()) {
      fail("genomgången hittade inga väntande resor fast startdatat har sådana");
      await page.keyboard.press("Escape");
    } else {
      const intro = await dialog.innerText();
      if (!intro.includes("bara reglerna")) fail("genomgången utan leverantör säger inte att bara reglerna används");
      await page.screenshot({ path: path.join(SHOTS, "ai-genomgang-start.png"), fullPage: true });
      await dialog.getByTestId("ai-review-start").click();
      const summary = dialog.getByTestId("ai-review-summary");
      await summary.waitFor({ timeout: 20000 });
      if (!(await summary.innerText()).startsWith("Klart:")) fail(`genomgången slutade med "${await summary.innerText()}"`);
      await page.screenshot({ path: path.join(SHOTS, "ai-genomgang-klar.png"), fullPage: true });
      await dialog.getByTestId("ai-review-close").click();
    }

    step("Resans klassningskort: Bedöm igen öppnar genomgången för resan");
    const open = (await api(page, "GET", "/trips?page_size=500")).items.find((t) => t.review_status === "needs_review" || t.review_status === "proposed");
    if (!open) throw new Error("ingen oavgjord demoresa");
    await visit(page, `/journal/${open.id}/`);
    const again = page.getByTestId("trip-ai-review").locator("visible=true").first();
    if (!(await again.innerText()).includes("Bedöm igen")) fail("klassningskortet saknar Bedöm igen");
    await again.click();
    await dialog.getByTestId("ai-review-start").click();
    await dialog.getByTestId("ai-review-summary").waitFor({ timeout: 10000 });
    await dialog.getByTestId("ai-review-close").click();
    const approved = (await api(page, "GET", "/trips?page_size=500")).items.find((t) => t.review_status === "approved");
    await visit(page, `/journal/${approved.id}/`);
    if (await page.getByTestId("trip-ai-review").locator("visible=true").count()) fail("Bedöm igen visas på en godkänd resa");

    step("Körjournalen: Gå igenom med AI för en vald resa öppnar genomgången");
    await visit(page, "/journal/");
    await page.getByTestId("trip-row").first().getByRole("checkbox").click();
    await page.getByTestId("bulk-ai-review").click();
    await dialog.waitFor({ timeout: 5000 });
    await dialog.getByText(/Det finns inga resor att gå igenom|skickas till|bara reglerna/).first().waitFor({ timeout: 8000 });
    await page.keyboard.press("Escape");
  });

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

  await section("avvikelse i granskningen", async () => {
    step("Granskning: avvikelsen visar resan, vad som inte stämmer och leder till resan; kön skiljer ärendena åt");
    if (!MISMATCH_TRIP || BACKWARDS_TRIPS.some((t) => !t)) return fail("demots avvikelseresor saknas i startdatat");
    const list = await api(page, "GET", `/review?status=open&limit=200&vehicle_id=${seed.vehicleId}`);
    const anomalies = list.items.filter((i) => i.kind === "anomaly");
    if (anomalies.some((i) => i.data?.type === "abnormal_consumption")) fail("demot har fortfarande ett förbrukningsärende");
    const one = anomalies.find((i) => i.data?.type === "odometer_mismatch");
    const two = anomalies.find((i) => i.data?.type === "odometer_backwards");
    if (!one || one.trip?.id !== MISMATCH_TRIP.id || one.logical_trip_id !== MISMATCH_TRIP.id) fail("avvikelsen om en resa följer inte kontraktet (trip, logical_trip_id)");
    if (!two || two.trip !== null || two.first_trip?.id !== BACKWARDS_TRIPS[0].id || two.second_trip?.id !== BACKWARDS_TRIPS[1].id) fail("avvikelsen om två resor följer inte kontraktet (first_trip, second_trip)");

    // Tangentraden på ett klassningsärende: A och P gäller där.
    await visit(page, "/review/");
    const card = page.getByTestId("review-current");
    await card.waitFor({ timeout: 8000 });
    if ((await card.getAttribute("data-kind")) !== "anomaly" && (await page.getByTestId("decide-work").count())) {
      const keys = await page.getByLabel("Kortkommandon").innerText();
      if (!keys.includes("Arbete") || !keys.includes("Privat")) fail(`tangentraden på ett klassningsärende saknar A/P: "${keys}"`);
      if (keys.includes("Slå samman")) fail("tangentraden visar J/N på ett klassningsärende");
    }

    // Kön: varje resebundet ärende har en andra rad med datum och från → till.
    const rows = page.locator('aside[aria-label="Kö"] li');
    const rowCount = await rows.count();
    if (!rowCount) fail("kön visas inte");
    const seenMeta = new Map();
    for (let i = 0; i < rowCount; i++) {
      const row = rows.nth(i);
      const meta = row.getByTestId("review-queue-meta");
      if (!(await meta.count())) {
        fail(`köraden "${(await row.innerText()).replace(/\s+/g, " ")}" saknar datum och från → till`);
        continue;
      }
      const text = await meta.innerText();
      if (!/^\d{4}-\d{2}-\d{2}.* · .+ → .+/.test(text)) fail(`köradens andra rad är "${text}"`);
      const title = (await row.locator("p").nth(1).innerText()).trim();
      if (seenMeta.has(title + text)) fail(`två köer med samma rubrik och samma andra rad: ${title} ${text}`);
      seenMeta.set(title + text, true);
      if ((await row.getAttribute("data-item-id")) === one?.id && !text.startsWith("2026-11-12")) fail(`avvikelsens kö-rad visar inte resans datum: "${text}"`);
      // Två resor: båda sträckorna, inte start på den ena och mål på den andra ("Hemmet → Hemmet").
      if ((await row.getAttribute("data-item-id")) === two?.id && (text.match(/→/g) ?? []).length !== 2) fail(`kö-raden för två resor visar inte båda sträckorna: "${text}"`);
    }

    // Avvikelsen om en resa.
    const c1 = await showAnomaly(page, "odometer_mismatch");
    if (c1) {
      const text = (await c1.innerText()).replace(/\s+/g, " ");
      for (const want of ["2026-11-12", placeOf(one.trip, "start"), placeOf(one.trip, "end"), "Ärende:", "21,4 km", "15 km", "I underlaget", "Om du inte gör något", "Avfärda"]) {
        if (!text.includes(want)) fail(`avvikelsekortet visar inte "${want}"`);
      }
      if ((await c1.getByTestId("anomaly-trip").first().getAttribute("data-trip-id", { timeout: 2000 }).catch(() => null)) !== MISMATCH_TRIP.id) fail("avvikelsekortet visar inte resan i en egen ruta");
      if (await c1.getByTestId("decide-work").count()) fail("avvikelsekortet visar Arbete/Privat");
      const keys = await page.getByLabel("Kortkommandon").innerText().catch(() => "");
      for (const k of ["Arbete", "Privat", "Godkänn"]) if (keys.includes(k)) fail(`tangentraden visar "${k}" på en avvikelse`);
      if (!(await c1.getByTestId("anomaly-help").count())) fail("hjälptexten om Markera som hanterat och Avfärda saknas");
      if (!(await c1.getByTestId("anomaly-open-trip").count())) fail("avvikelsekortet saknar huvudknappen Öppna resan");
      if (text.includes("≈")) fail("avvikelsens resa visar en uppskattad starttid (≈), som den riktiga kön inte har");
      await page.screenshot({ path: path.join(SHOTS, "granskning-avvikelse.png"), fullPage: true });
      // A, P och G gör ingenting på en avvikelse.
      const item = await c1.getAttribute("data-item-id");
      for (const k of ["a", "p", "g"]) await page.keyboard.press(k);
      await page.waitForTimeout(500);
      if ((await page.getByTestId("review-current").getAttribute("data-item-id")) !== item) fail("A/P/G bytte ärende på en avvikelse");
      if ((await api(page, "GET", `/trips/${MISMATCH_TRIP.id}`)).trip.category !== MISMATCH_TRIP.category) fail("A/P/G ändrade avvikelsens resa");
      if (await c1.getByTestId("anomaly-open-trip").count()) await c1.getByTestId("anomaly-open-trip").click();
      await page.waitForURL(new RegExp(`/journal/${MISMATCH_TRIP.id}/?$`), { timeout: 8000 }).catch(() => fail(`huvudknappen ledde till ${page.url()}`));
    }

    // Avvikelsen om två resor.
    const c2 = await showAnomaly(page, "odometer_backwards");
    if (c2) {
      const text = (await c2.innerText()).replace(/\s+/g, " ");
      if ((await c2.getByTestId("anomaly-trip").count()) !== 2) fail("avvikelsen om två resor visar inte båda resorna");
      for (const want of ["Första resan", "Andra resan", "2026-11-16", placeOf(two.first_trip, "end"), placeOf(two.second_trip, "start")]) {
        if (!text.toLowerCase().includes(want.toLowerCase())) fail(`kortet för två resor visar inte "${want}"`);
      }
      if (!/48\s?803/.test(text) || !/48\s?799/.test(text)) fail("kortet för två resor visar inte mätarställningarna som inte går ihop");
      if (text.includes("≈")) fail("avvikelsens resor visar uppskattade starttider (≈)");
      const href = (id) => c2.getByTestId(id).getAttribute("href", { timeout: 2000 }).catch(() => null);
      const hrefs = [await href("anomaly-open-trip"), await href("anomaly-open-trip-2")];
      BACKWARDS_TRIPS.forEach((t, i) => {
        if (!hrefs[i]?.includes(t.id)) fail(`knapp ${i + 1} leder till ${hrefs[i]}, inte till resan ${t.id}`);
      });
      await page.screenshot({ path: path.join(SHOTS, "granskning-avvikelse-tva-resor.png"), fullPage: true });
    }

    // En privat resa (endast tjänsteresor redovisas): avvikelsen om den resan räknas inte längre.
    const before = await reviewTotal();
    await api(page, "PATCH", `/trips/${MISMATCH_TRIP.id}`, { category: "private" });
    if ((await reviewTotal()) !== before - 1) fail("avvikelsen om en privat resa ligger kvar");
    await api(page, "PATCH", `/trips/${MISMATCH_TRIP.id}`, { category: MISMATCH_TRIP.category });
    if ((await reviewTotal()) !== before) fail("avvikelsen kom inte tillbaka när resan blev tjänst igen");
  });

  await section("avvikelser: råd efter källan, saknad resa och äldre typer", async () => {
    step("Granskning: råden följer resans källa, kortet utan resa och äldre ärendetyper får förklaring");
    // Ärenden som demot inte har läggs in i demots tillstånd (webbläsarens lagring) och tas bort efteråt.
    const KEY = "drivelog.demo.state.v1";
    const saved = await page.evaluate((k) => window.localStorage.getItem(k), KEY);
    if (!saved) return fail("demots tillstånd finns inte i webbläsarens lagring");
    const state = JSON.parse(saved);
    const [a, b] = BACKWARDS_TRIPS;
    const imported = trips.find((t) => t.category === "work" && t.started_at && t.ended_at && t.id !== MISMATCH_TRIP.id && !BACKWARDS_TRIPS.includes(t));
    if (!imported || !state.trips[imported.id]) return fail("ingen tjänsteresa att göra till importerad");
    state.trips[imported.id].trip.source = "volvo_import";
    const elapsed = Math.round((Date.parse(imported.ended_at) - Date.parse(imported.started_at)) / 60000);
    const base = { vehicle_id: seed.vehicleId, kind: "anomaly", status: "open", snoozed_until: null, created_at: seed.now, resolved_at: null, resolution: {}, kind_label: "Avvikelse" };
    const add = (id, title, data, tripIds, extra = {}) => state.review.unshift({ ...base, id, title, detail: "Text från servern", data, logical_trip_id: tripIds.length === 1 ? tripIds[0] : null, trip_ids: tripIds, ...extra });
    const ids = {
      time: "e2e00000-0000-4000-8000-000000000001",
      dist: "e2e00000-0000-4000-8000-000000000002",
      legacy: "e2e00000-0000-4000-8000-000000000003",
      stale: "e2e00000-0000-4000-8000-000000000004",
      gone: "e2e00000-0000-4000-8000-000000000005",
      mergeOverlap: "e2e00000-0000-4000-8000-000000000006",
      part: "e2e00000-0000-4000-8000-000000000007",
    };
    add(ids.time, "Körtiden är längre än resans längd i tid", { type: "time_conflict", started_at: imported.started_at, ended_at: imported.ended_at, driving_minutes: elapsed + 45, elapsed_minutes: elapsed }, [imported.id]);
    add(ids.dist, "Körsträckan stämmer inte med mätarställningen", { type: "odometer_mismatch", distance_km: imported.distance_km, reported_km: imported.distance_km, odometer_km: imported.distance_km + 20 }, [imported.id]);
    add(ids.legacy, "Motstridiga mätarställningar", { type: "odometer_overlap" }, [a.id, b.id]);
    add(ids.stale, "Fordonet har inte rapporterat nya uppgifter", { type: "stale_data" }, []);
    add(ids.gone, "Körsträckan stämmer inte med mätarställningen", { type: "odometer_mismatch", distance_km: 21.4, started_at: "2026-11-10T07:15:00Z" }, ["00000000-0000-4000-8000-0000deadbeef"], { demo_trip_gone: true });
    add(ids.mergeOverlap, "Överlappande resor", { type: "overlap", first_trip_id: a.id, second_trip_id: b.id }, [a.id, b.id]);
    // Råresan är bara en del av den sammanslagna resan i rutan.
    add(ids.part, "Körsträckan stämmer inte med mätarställningen", { type: "odometer_mismatch", distance_km: 8.0, odometer_km: 20, reported_km: imported.distance_km, part_of_trip: true, part_distance_km: 8.0, trip_distance_km: imported.distance_km, part_started_at: imported.started_at, part_ended_at: imported.ended_at }, [imported.id]);
    await page.evaluate(([k, v]) => window.localStorage.setItem(k, v), [KEY, JSON.stringify(state)]);
    const restore = async () => {
      await page.evaluate(([k, v]) => window.localStorage.setItem(k, v), [KEY, saved]);
      await visit(page, "/review/");
    };
    try {
      const textOf = async (card) => (await card.innerText()).replace(/\s+/g, " ");
      // Tiderna kommer ur importen och går inte att ändra: inget råd om att ändra starttiden.
      const time = await showAnomaly(page, null, ids.time);
      if (time) {
        const t = await textOf(time);
        if (/ändra starttiden|ange rätt starttid/i.test(t)) fail(`kortet för en importerad resa råder att ändra starttiden: "${t.slice(0, 200)}"`);
        if (!t.includes("ur Volvos körjournal")) fail("kortet säger inte att klockslagen kommer ur Volvos körjournal");
      }
      // En importerad resa: en ny import ger samma värden.
      const dist = await showAnomaly(page, null, ids.dist);
      if (dist) {
        const t = await textOf(dist);
        if (t.includes("importera Volvos körjournal för dagen")) fail("kortet råder att importera Volvos körjournal för en resa som redan är importerad");
        if (!t.includes("en ny import ger samma värden")) fail("kortet säger inte att en ny import ger samma värden");
      }
      // En delsträcka: texten säger att det gäller en del av resan, och underlaget redovisar hela resans sträcka.
      const part = await showAnomaly(page, null, ids.part);
      if (part) {
        const t = await textOf(part);
        if (!t.includes("delsträcka av resan")) fail("kortet säger inte att avvikelsen gäller en delsträcka av resan");
        if (!t.includes("8,0 km")) fail("kortet visar inte delsträckans sträcka");
        if (t.includes("Delsträckans sträcka är 8,0 km") && !t.includes(`redovisar ${imported.distance_km.toFixed(1).replace(".", ",")} km`)) fail("kortet säger inte att underlaget redovisar hela resans sträcka");
      }
      // Omräkningens äldre typ odometer_overlap förklaras som mätarställningar som går bakåt.
      const legacy = await showAnomaly(page, null, ids.legacy);
      if (legacy) {
        if (!(await legacy.getByTestId("anomaly-explanation").count())) fail("odometer_overlap visas utan förklaring (Vad du gör, Om du inte gör något)");
        if (!(await textOf(legacy)).includes("Mätarställningen går bakåt") && !/48\s?799/.test(await textOf(legacy))) fail("odometer_overlap förklaras inte med mätarställningarna");
      }
      // Sammanslagningens äldre överlapp där tiderna inte överlappar: förklaras med mätarställningen.
      const merged = await showAnomaly(page, null, ids.mergeOverlap);
      if (merged) {
        const t = await textOf(merged);
        if (t.includes("överlappar i tid")) fail(`överlappet förklaras som ett överlapp i tid fast tiderna inte överlappar: "${t.slice(0, 200)}"`);
        if (!/48\s?803/.test(t)) fail("överlappet förklaras inte med mätarställningarna");
      }
      // Insamlingens stale_data har samma väg till Integrationer som stale_vehicle_data.
      const stale = await showAnomaly(page, null, ids.stale);
      if (stale) {
        const href = await stale.getByTestId("anomaly-open-place").getAttribute("href", { timeout: 2000 }).catch(() => null);
        if (!href?.includes("/integrations")) fail(`stale_data saknar knappen till Integrationer (${href})`);
      }
      // Resan saknas i svaret: bara meddelandet (med datum) och Avfärda, inga råd om resan.
      const gone = await showAnomaly(page, null, ids.gone);
      if (gone) {
        const t = await textOf(gone);
        if (!(await gone.getByTestId("anomaly-trip-missing").count())) fail("kortet säger inte att resan inte kunde visas");
        if (await gone.getByTestId("anomaly-explanation").count()) fail("kortet utan resa visar ändå Vad du gör och Om du inte gör något");
        if (await gone.getByTestId("anomaly-resolve").count()) fail("kortet utan resa har knappen Markera som hanterat");
        if (!t.includes("2026-11-10")) fail("kortet utan resa visar inte datumet ur ärendet");
        await page.screenshot({ path: path.join(SHOTS, "granskning-avvikelse-saknad-resa.png"), fullPage: true });
        const row = page.locator(`aside[aria-label="Kö"] li[data-item-id="${ids.gone}"]`);
        const meta = (await row.getByTestId("review-queue-meta").innerText({ timeout: 2000 }).catch(() => "")).trim();
        if (!meta.startsWith("2026-11-10")) fail(`kö-raden för ärendet utan resa visar inte datumet ("${meta}")`);
      }
    } finally {
      await restore();
    }
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

    step("Integrationer: Outlook-panelen visar Kalenderlänk (ICS) utan länk; Spara ger demots klartextfel");
    await page.getByTestId("configure-microsoft").click();
    const ics = page.getByTestId("ics-section");
    await ics.waitFor({ timeout: 8000 });
    await settled(page, "Outlook-panelen");
    if (!(await page.getByText("Kalenderlänk (ICS)", { exact: true }).count())) fail("rubriken Kalenderlänk (ICS) saknas");
    const icsInput = page.getByTestId("ics-link-input").locator("input");
    if ((await icsInput.getAttribute("type")) !== "password") fail("kalenderlänkens fält är inget lösenordsfält");
    if (await icsInput.inputValue()) fail("kalenderlänkens fält är förifyllt");
    if (!(await page.getByTestId("ics-save").isDisabled())) fail("Spara är aktiv med tomt fält");
    await page.screenshot({ path: path.join(SHOTS, "kalenderlank.png"), fullPage: true });
    await icsInput.fill("https://calendar.example.com/owa/calendar/demo/calendar.ics");
    await page.getByTestId("ics-save").click();
    await expectText(page, "Kalenderlänken går inte att prova i demot");
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
    // Avvikelsernas resor har starttid från bilen i demot (applyV132b), så de räknas inte här.
    const est = trips.find((t) => t.start_time_source === "estimated" && t.started_at && t.id !== MISMATCH_TRIP?.id && !BACKWARDS_TRIPS.includes(t));
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
    // Adressen i underlaget: fältet och knappen "Hämta adressen från kartan" (demot slår inte upp något).
    const address = page.getByTestId("place-address");
    await address.fill("");
    await page.getByTestId("place-address-missing").waitFor({ timeout: 5000 }).catch(() => fail("tom adress visas inte som saknad"));
    await page.getByTestId("place-address-lookup").click();
    await page.getByText("Adressen är ifylld från kartan").waitFor({ timeout: 5000 }).catch(() => fail("adressen fylldes inte i från kartan"));
    if ((await address.inputValue()) !== target.address) fail(`adressfältet fick ${await address.inputValue()}`);
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


  await section("sammanslagning", async () => {
    step("Inställningar → Sammanslagning: grupperna Tjänsteresor och Privata resor, spara privatgränsen");
    await visit(page, "/settings/?sektion=sammanslagning", "sammanslagning");
    const panel = page.getByTestId("settings-panel-sammanslagning");
    for (const t of ["Tjänsteresor", "Privata resor", "Så här avgörs ett stopp"]) {
      if (!(await panel.getByText(t, { exact: true }).count())) fail(`"${t}" saknas i sammanslagningen`);
    }
    if (!(await panel.getByText("Stopp hemma eller vid en privat plats slås aldrig ihop.").count())) fail("regeln om stopp hemma saknas");
    if (await page.getByTestId("merge-legacy").count()) fail("demot visar det tidigare sättet trots by_category");
    await page.locator("#merge-private-auto").fill("10");
    await page.locator("#merge-private-ask").fill("5");
    await expectText(page, "Gränsen för fråga för privata resor måste vara minst lika stor");
    await page.locator("#merge-private-ask").fill("20");
    await panel.getByRole("button", { name: "Spara" }).click();
    await expectText(page, "Reglerna för sammanslagning är sparade.");
    const merge = (await api(page, "GET", "/settings")).settings.merge;
    if (merge.private_auto_max_minutes !== 10 || merge.private_ask_max_minutes !== 20 || merge.by_category !== true) {
      fail(`sammanslagningen sparades fel: ${JSON.stringify(merge)}`);
    }
  });

  await section("uppehåll", async () => {
    step("Resans sida: uppehåll i hämtningen från Volvo – när, hur många km och vad man gör");
    const all = (await api(page, "GET", "/trips?page_size=500")).items;
    const gap = all.find((t) => t.collection_gap);
    if (!gap) throw new Error("ingen demoresa med uppehåll i hämtningen");
    await visit(page, `/journal/${gap.id}/`, "resa-uppehall");
    const note = page.getByTestId("collection-gap-note").locator("visible=true");
    await note.first().waitFor({ timeout: 8000 });
    const text = await note.first().innerText();
    if (!text.includes("Uppehåll i hämtningen från Volvo")) fail("rubriken för uppehållet saknas");
    if (!/ökade mätaren med 7 km/.test(text)) fail(`uppehållet säger inte hur mycket mätaren ökade: ${text.slice(0, 160)}`);
    if (!(await note.first().getByRole("link", { name: "Importera körjournal" }).count())) fail("knappen Importera körjournal saknas");
    await note.first().getByRole("button", { name: "Dela upp" }).click();
    await page.getByRole("dialog").waitFor({ timeout: 5000 });
    await page.keyboard.press("Escape");
  });

  await section("importerade resor", async () => {
    step("Importerad resa utan positioner: Positionen slås upp; platsen visar samma läge");
    await visit(page, `/journal/${importedTripId(2)}/`, "resa-importerad-utan-karta");
    await expectText(page, "Positionen slås upp");
    if (await page.getByText("Inga koordinater för resan").count()) fail("den gamla texten Inga koordinater visas");
    await visit(page, "/places/");
    await page.getByTestId("place-row").filter({ hasText: "Exempel Snickeri AB" }).first().click();
    await page.getByTestId("place-position-lookup").waitFor({ timeout: 8000 }).catch(() => fail("platsen visar inte att positionen slås upp"));
    await page.keyboard.press("Escape");

    step("Ta bort en importerad resa: bekräftelsen, kvittot och resan försvinner ur körjournalen");
    const id = importedTripId(1);
    await visit(page, `/journal/${id}/`);
    await page.getByTestId("delete-trip").click();
    const dialog = page.getByTestId("delete-trip-dialog");
    await dialog.waitFor({ timeout: 5000 });
    const text = await dialog.innerText();
    if (!text.includes("kommer resan tillbaka")) fail("bekräftelsen säger inte att resan kommer tillbaka vid ny import");
    if (!text.includes("Kilometrarna räknas därefter som övrig körning")) fail("bekräftelsen säger inte vad kilometrarna blir");
    await page.screenshot({ path: path.join(SHOTS, "ta-bort-resa.png"), fullPage: true });
    await page.getByTestId("delete-trip-confirm").click();
    await expectText(page, "Resan är borttagen.");
    await page.waitForURL(/\/journal\/?$/, { timeout: 8000 }).catch(() => fail("borttagningen ledde inte till körjournalen"));
    const left = (await api(page, "GET", "/trips?page_size=500")).items;
    if (left.some((t) => t.id === id)) fail("den borttagna resan finns kvar i körjournalen");
    const history = (await api(page, "GET", `/trips/${id}`)).history;
    if (history[0]?.action !== "trip.import_delete") fail("borttagningen syns inte i resans historik");

    step("Körjournalen: Ta bort med en resa från bilen vald – den kan inte tas bort");
    await visit(page, "/journal/");
    const row = page.getByTestId("trip-row").first();
    await row.getByRole("checkbox").click();
    await page.getByTestId("bulk-delete").click();
    const bulk = page.getByTestId("bulk-delete-dialog");
    await bulk.waitFor({ timeout: 5000 });
    if (!(await bulk.innerText()).includes("kan inte tas bort")) fail("massfältet säger inte att resor från bilen inte kan tas bort");
    await page.screenshot({ path: path.join(SHOTS, "ta-bort-flera.png"), fullPage: true });
    await bulk.getByTestId("bulk-delete-close").click();
  });

  await section("förhandsvisning", async () => {
    step("Export → oktober: Adress saknas i cellen och tiden bryts aldrig");
    await visit(page, "/export/?manad=2026-10", "export-oktober");
    const missing = page.getByTestId("preview-address-missing").locator("visible=true");
    await missing.first().waitFor({ timeout: 8000 }).catch(() => fail("förhandsvisningen visar inte Adress saknas"));
    if ((await missing.count()) && !(await missing.first().innerText()).includes("Adress saknas – lägg till under Platser")) fail("texten för saknad adress stämmer inte");
    const times = page.getByTestId("preview-time");
    const n = await times.count();
    if (!n) fail("förhandsvisningen har inga tidsceller");
    for (let i = 0; i < n; i++) {
      const lines = await lineCount(times.nth(i));
      if (lines > 1) {
        fail(`tiden "${await times.nth(i).innerText()}" bryts på ${lines} rader`);
        break;
      }
    }
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

  await section("granskning i rask takt", async () => {
    step("Granskning: A/P i rask takt – nästa ärende direkt och rätt svar på rätt resa");
    await visit(page, "/review/");
    const card = page.getByTestId("review-current");
    await card.waitFor({ timeout: 8000 });
    const answered = new Map();
    for (let n = 0; n < 10 && answered.size < 4; n++) {
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
      await page.waitForFunction(
        (prev) => document.querySelector('[data-testid="review-current"]')?.getAttribute("data-item-id") !== prev,
        item,
        { timeout: 1500 },
      );
    }
    if (answered.size < 2) fail(`bara ${answered.size} resor att klassa i granskningen`);
    await page.waitForTimeout(600);
    for (const [id, category] of answered) {
      const t = (await api(page, "GET", `/trips/${id}`)).trip;
      if (t.category !== category) fail(`resan ${id} fick ${t.category}, väntade ${category}`);
    }
  });


  await section("månad i förväg", async () => {
    step("Export: Skapa underlaget nu – samma kontroller (väntar något skapas ingen fil), sedan Skapat innan månaden var slut");
    await visit(page, "/export/");
    const row = page.getByTestId("month-row").filter({ has: page.getByTestId("month-create-now") });
    await row.first().waitFor({ timeout: 8000 });
    if ((await row.first().getAttribute("data-month")) !== "2026-11") fail("knappen finns inte på innevarande månad");
    await expectText(page, "Månaden pågår. Underlaget skapas av sig självt när den är slut.");
    // Minst en obesvarad resa i november, oavsett vad tidigare avsnitt har besvarat.
    const novTrips = (await api(page, "GET", "/trips?page_size=500&from=2026-11-01&to=2026-11-30")).items;
    const reopen = novTrips.find((t) => t.category === "work" && t.review_status === "approved" && t.approval_source === "user") ?? novTrips[0];
    await api(page, "PATCH", `/trips/${reopen.id}`, { category: "undecided" });
    await visit(page, "/export/");
    await page.getByTestId("month-create-now").click();
    const dialog = page.getByTestId("month-create-now-dialog");
    await dialog.waitFor({ timeout: 5000 });
    const text = await dialog.innerText();
    if (!text.includes("Skapa underlaget för november nu?")) fail("dialogens rubrik stämmer inte");
    if (!text.includes("Vid månadsskiftet kontrollerar DriveLog")) fail("dialogen säger inte vad som händer vid månadsskiftet");
    await page.screenshot({ path: path.join(SHOTS, "manad-skapa-nu.png"), fullPage: true });
    await page.getByTestId("month-create-now-confirm").click();
    await expectText(page, "Underlaget kan inte skapas än – se vad som väntar.");
    if ((await api(page, "GET", `/exports?vehicle_id=${seed.vehicleId}`)).some((e) => e.origin === "month_close" && e.params?.label === "2026-11")) {
      fail("en fil skapades trots obesvarade frågor");
    }
    // Det som väntar besvaras (som ägaren gör i Granskning och månadens genomgång).
    const open = (await api(page, "GET", "/trips?page_size=500&needs_review=true&from=2026-11-01&to=2026-11-30")).items.map((t) => t.id);
    if (open.length) await api(page, "POST", "/trips/bulk", { trip_ids: open, category: "private" });
    const nov = (await api(page, "GET", "/month-close?month=2026-11")).months[0];
    for (const r of nov.review_rows.filter((x) => x.approval_source === "assumed")) await api(page, "POST", `/notifications/review-rows/${r.trip_id}`, { verdict: "ok" });
    await visit(page, "/export/");
    await page.getByTestId("month-create-now").click();
    await page.getByTestId("month-create-now-confirm").click();
    await expectText(page, "Underlaget för november är skapat.");
    const state = page.getByTestId("month-row").filter({ hasText: "November 2026" }).getByTestId("month-state-text");
    await state.waitFor({ timeout: 8000 });
    const said = await state.innerText();
    if (!/^Skapat .+, innan månaden var slut\./.test(said)) fail(`månadskortet säger "${said}"`);
    const after = (await api(page, "GET", "/month-close?month=2026-11")).months[0];
    if (after.state !== "exported" || !after.closed_early) fail(`månaden blev ${after.state} (closed_early ${after.closed_early})`);
    if (await page.getByTestId("month-create-now").count()) fail("knappen finns kvar när underlaget är skapat");
    await page.screenshot({ path: path.join(SHOTS, "manad-skapad-i-forvag.png"), fullPage: true });
  });

  await section("förslag och AI", async () => {
    step("Inställningar → Förslag och AI: två underlagsnivåer med Fullständigt valt, Om mitt arbete sparas");
    await visit(page, "/settings/?sektion=forslag", "ai-underlag");
    const panel = page.getByTestId("settings-panel-forslag");
    const full = panel.getByRole("radio", { name: /Fullständigt underlag/ });
    const minimal = panel.getByRole("radio", { name: /Minimalt/ });
    if (!(await full.count()) || !(await minimal.count())) fail("underlagsnivåerna saknas");
    else if (!(await full.isChecked())) fail("Fullständigt underlag är inte valt");
    if (await panel.getByText("Gatuadresser", { exact: true }).count()) fail("minimal-valen visas vid Fullständigt");
    if (!(await panel.getByText(/Privata bokningar skickas bara som tid/).count())) fail("radiotexten om privata bokningar saknas");
    await panel.locator("label").filter({ hasText: "Bara platsens namn och ort" }).click();
    if (!(await panel.getByText("Gatuadresser", { exact: true }).count())) fail("minimal-valen visas inte vid Minimalt");
    await panel.locator("label").filter({ hasText: "Fullständigt underlag" }).click();
    if (!(await full.isChecked())) fail("Fullständigt underlag gick inte att välja igen");
    await page.locator("#ai-about-work").fill("Jag är servicetekniker på Exempel Åkeri AB och besöker kunder i länet.");
    await panel.getByRole("button", { name: "Spara" }).click();
    await expectText(page, "Inställningarna för förslag och AI är sparade.");
    const ai = (await api(page, "GET", "/settings")).settings.ai;
    if (ai.context_level !== "full" || !String(ai.about_work).startsWith("Jag är servicetekniker")) fail(`AI-inställningarna sparades fel: ${JSON.stringify(ai)}`);
    await visit(page, "/integrations/");
    const integ = await api(page, "GET", "/integrations/ai");
    if (integ.privacy?.context_level !== "full") fail("integrationen säger inte Fullständigt underlag");
  });

  await section("lunchregeln", async () => {
    step("Inställningar → Lunchregeln: mönstret, valideringen och sparat");
    await visit(page, "/settings/?sektion=lunch", "lunchregeln");
    const panel = page.getByTestId("settings-panel-lunch");
    const pattern = page.getByTestId("lunch-pattern");
    const text = await pattern.innerText();
    for (const t of ["Resan börjar vid ett av era kontor en arbetsdag.", "högst 10 minuter och 8 km", "mellan 20 och 45", "mellan 11:00 och 13:00", "Klockslaget ensamt gör aldrig en resa privat"]) {
      if (!text.replace(/\s+/g, " ").includes(t)) fail(`lunchmönstret saknar "${t}"`);
    }
    if (await page.getByTestId("lunch-office-missing").count()) fail("varningen om saknat kontor visas fast demot har ett kontor");
    await page.locator("#lunch-start").fill("13:30");
    await expectText(page, "Lunchfönstrets början måste ligga före slutet.");
    await page.locator("#lunch-start").fill("11:15");
    await page.locator("#lunch-max-stop").fill("40");
    if (!(await pattern.innerText()).replace(/\s+/g, " ").includes("mellan 20 och 40")) fail("mönstret följer inte den ändrade gränsen");
    await panel.getByRole("button", { name: "Spara" }).click();
    await expectText(page, "Lunchregeln är sparad.");
    const lunch = (await api(page, "GET", "/settings")).settings.lunch;
    if (lunch.window_start !== "11:15" || lunch.max_stop_minutes !== 40 || lunch.enabled !== true) fail(`lunchregeln sparades fel: ${JSON.stringify(lunch)}`);
  });

  await section("kalender och tjänsteställe", async () => {
    step("Kalender: Läs hela bokningen är påslagen och domänfältet är ifyllt; Tjänsteställe: hem ↔ kontor");
    await visit(page, "/settings/?sektion=kalender", "kalender-hela-bokningen");
    const body = page.locator("#cal-body");
    if ((await body.getAttribute("aria-checked")) !== "true") fail("Läs hela bokningen är inte påslagen");
    if (!(await page.getByText("Läs hela bokningen – beskrivning och deltagare").count())) fail("texten Läs hela bokningen saknas");
    if ((await page.locator("#cal-domain").inputValue()) !== "exempelakeri.se") fail("domänfältet är inte ifyllt");
    if (!(await page.getByText(/Ifylld av DriveLog ur kalendern/).count())) fail("domänfältet säger inte att DriveLog fyllde i det");
    await page.locator("#cal-domain").fill("Exempel Åkeri");
    await expectText(page, "Ange bara domänen, till exempel exempelakeri.se.");
    await page.getByTestId("settings-panel-kalender").getByRole("button", { name: "Ångra" }).click();
    await visit(page, "/settings/?sektion=tjanstestalle", "tjanstestalle-hem-kontor");
    if (!(await page.getByText("Resor mellan hemmet och våra kontor är tjänsteresor och godkänns automatiskt").count())) fail("valet hem ↔ kontor saknas");
    if ((await page.locator("#auto-home-office").getAttribute("aria-checked")) !== "true") fail("hem ↔ kontor är inte påslaget i demot");
  });

  await section("ärende ur kalendern", async () => {
    step("Klassningskortet: Ur kalendern och Ärendet stämmer inte; månadens genomgång visar samma");
    await visit(page, `/journal/${CALENDAR_TRIP}/`, "resa-ur-kalendern");
    const badge = page.getByTestId("purpose-badge").locator("visible=true");
    if (!(await badge.count()) || (await badge.first().innerText()).trim() !== "Ur kalendern") fail("märket Ur kalendern saknas på resan");
    if (!(await page.getByTestId("reject-calendar-purpose").locator("visible=true").count())) fail("knappen Ärendet stämmer inte saknas på resan");
    await visit(page, "/m/month/2026-11/", "manad-ur-kalendern");
    if (!(await page.getByTestId("month-review-calendar").count())) fail("månadens genomgång märker inte ärendet ur kalendern");
    await visit(page, "/export/");
    const note = page.getByTestId("month-note-purpose_from_calendar");
    await note.waitFor({ timeout: 8000 });
    if (!(await note.innerText()).includes("Ärende ur kalendern: 1 resa")) fail("månadskortet saknar raden Ärende ur kalendern");
    await note.getByRole("button", { name: "Se resorna" }).click();
    await note.getByTestId("reject-calendar-purpose").click();
    await expectText(page, "Kalenderns ärende är borttaget");
    await note.waitFor({ state: "detached", timeout: 8000 }).catch(() => fail("raden Ärende ur kalendern finns kvar efter rättelsen"));
    const d = await api(page, "GET", `/trips/${CALENDAR_TRIP}`);
    if (d.trip.purpose_confirmed) fail("ärendet är fortfarande bekräftat efter Ärendet stämmer inte");
    if (!d.purpose_history.length || d.purpose_history.find((p) => p.is_current)?.from_calendar) fail("ärendet står kvar som ur kalendern");
  });

  await section("version", async () => {
    step("Versionen: DriveLog demo längst ned i sidomenyn leder till Inställningar → Om DriveLog");
    await visit(page, "/journal/");
    const link = page.getByTestId("version-link").locator("visible=true");
    await link.first().waitFor({ timeout: 8000 });
    if ((await link.first().innerText()).trim() !== "DriveLog demo") fail(`sidomenyn visar "${await link.first().innerText()}"`);
    await link.first().click();
    // Klientnavigering: Nexts ruttutropare (role="alert") får sidans titel, så settled() används inte här.
    await page.waitForURL(/\/settings\/?\?sektion=om/, { timeout: 8000 });
    await page.getByTestId("about-card").waitFor({ timeout: 8000 });
    if ((await page.getByTestId("about-version").innerText()).trim() !== "DriveLog demo") fail("Om DriveLog visar inte demots version");
    if (!(await page.getByTestId("about-changelog").getByRole("link", { name: "GitHub" }).count())) fail("Om DriveLog saknar länken till ändringsloggen");
    await page.screenshot({ path: path.join(SHOTS, "om-drivelog.png"), fullPage: true });
  });

  await section("felsökningsfil", async () => {
    step("Integrationer → Volvo: Ladda ner felsökningsfil ger en fil utan VIN, regnr, positioner och adresser");
    await visit(page, "/integrations/");
    await page.getByTestId("configure-volvo").click();
    const btn = page.getByTestId("volvo-diagnostics");
    await btn.waitFor({ timeout: 8000 });
    if (!(await page.getByText(/utan chassinummer, registreringsnummer, positioner och adresser/).count())) fail("texten om vad filen saknar visas inte");
    const [download] = await Promise.all([page.waitForEvent("download", { timeout: 10000 }), btn.click()]);
    if (!/^drivelog-felsokning-\d{4}-\d{2}-\d{2}\.json$/.test(download.suggestedFilename())) fail(`filnamnet blev ${download.suggestedFilename()}`);
    const text = readFileSync(await download.path(), "utf8");
    const file = JSON.parse(text);
    if (!Array.isArray(file.observations) || !file.observations.length || !Array.isArray(file.trips)) fail("felsökningsfilen saknar avläsningar eller resor");
    if (/latitude|longitude|vin|registration|address/i.test(text)) fail("felsökningsfilen innehåller positioner, VIN, regnr eller adresser");
    const v = seed.vehicles?.[0] ?? {};
    for (const secret of [v.registration_number, v.vin].filter(Boolean)) if (text.includes(secret)) fail("felsökningsfilen innehåller fordonets uppgifter");
    await expectText(page, "Felsökningsfilen är nedladdad.");
    await page.screenshot({ path: path.join(SHOTS, "volvo-felsokning.png"), fullPage: true });
    await page.keyboard.press("Escape");
  });

  await section("förbrukning", async () => {
    step("Resans sida: uppskattad bensinförbrukning med ≈ och förklaringen");
    const est = (await api(page, "GET", "/trips?page_size=500")).items.find((t) => t.fuel_quality === "estimated" && t.fuel_liters != null);
    if (!est) fail("ingen demoresa har uppskattad förbrukning");
    else {
      await visit(page, `/journal/${est.id}/`, "resa-forbrukning");
      const value = page.locator(".value-estimated", { hasText: / l$/ }).locator("visible=true").first();
      await value.waitFor({ timeout: 8000 });
      if (!(await value.innerText()).startsWith("≈")) fail(`förbrukningen visas utan ≈: ${await value.innerText()}`);
      await value.focus();
      await expectText(page, "Uppskattad ur bilens egna avläsningar – inte uppmätt per resa.");
    }
  });

  await section("notis om ny version", async () => {
    step("Inställningar → Notiser: Notis när DriveLog har uppdaterats, av och på igen; hela adressen på låsskärmen");
    await visit(page, "/settings/?sektion=notiser", "notiser-ny-version");
    const sw = page.locator("#ntf-release");
    await sw.waitFor({ timeout: 8000 });
    if ((await sw.getAttribute("aria-checked")) !== "true") fail("notisen om ny version är inte påslagen");
    if (!(await page.getByText("En notis per ny version, med de viktigaste nyheterna.").count())) fail("förklaringen till notisen om ny version saknas");
    if (!(await page.getByText("Notiserna visar hela adressen för platser utan namn.", { exact: false }).count())) fail("låsskärmens förklaring nämner inte hela adressen");
    await sw.click();
    const panel = page.getByTestId("settings-panel-notiser");
    await panel.getByRole("button", { name: "Spara" }).click();
    await expectText(page, "Inställningarna för notiser är sparade.");
    if ((await api(page, "GET", "/settings")).settings.notifications.kinds.release !== false) fail("valet sparades inte");
    await sw.click();
    await panel.getByRole("button", { name: "Spara" }).click();
    await page.waitForTimeout(400);
    if ((await api(page, "GET", "/settings")).settings.notifications.kinds.release !== true) fail("valet gick inte att slå på igen");
  });

  await section("företagslogga", async () => {
    step("Inställningar → Milersättning och export: ladda upp loggan, fel filtyp avvisas, byt och ta bort");
    await visit(page, "/settings/?sektion=ersattning");
    const card = page.getByTestId("export-logo");
    await card.waitFor({ timeout: 8000 });
    if (!(await page.getByText("Visa loggan i underlaget").count())) fail("valet Visa loggan i underlaget saknas");
    const input = page.getByTestId("export-logo-input");
    await input.setInputFiles({ name: "logga.svg", mimeType: "image/svg+xml", buffer: Buffer.from("<svg xmlns='http://www.w3.org/2000/svg'/>") });
    await card.getByTestId("export-logo-error").waitFor({ timeout: 8000 });
    // Rätt namn och typ men fel innehåll: servern (demot) kontrollerar filens innehåll.
    await input.setInputFiles({ name: "logga.png", mimeType: "image/png", buffer: Buffer.from("<html>inte en bild</html>") });
    await expectText(page, "Loggan måste vara en PNG- eller JPEG-bild.");
    await input.setInputFiles({ name: "logga.png", mimeType: "image/png", buffer: PNG });
    await card.getByTestId("export-logo-preview").waitFor({ timeout: 8000 });
    await expectText(page, "Loggan är uppladdad.");
    if (await card.getByTestId("export-logo-error").count()) fail("felet står kvar efter en lyckad uppladdning");
    await page.screenshot({ path: path.join(SHOTS, "foretagslogga.png"), fullPage: true });
    await card.getByRole("button", { name: "Ta bort företagsloggan" }).click();
    await card.getByRole("button", { name: "Ja, ta bort loggan" }).click();
    await card.getByTestId("export-logo-upload").waitFor({ timeout: 8000 });
    const logo = (await api(page, "GET", "/settings")).settings.export.logo;
    if (logo && Object.keys(logo).length) fail("loggan finns kvar efter Ta bort");
  });

  await section("återställ", async () => {
    step("Återställ demot: tillbaka till startdatat");
    await visit(page, "/");
    await page.getByRole("button", { name: "Återställ demot" }).click();
    await page.waitForURL(`${BASE}/`);
    await settled(page, "efter återställning");
    if ((await page.getByTestId("get-started-step-home").getAttribute("data-done")) !== "false") fail("bostaden är fortfarande markerad efter återställning");
    if ((await reviewTotal()) !== startReviewTotal) fail(`granskningen återställdes inte (${await reviewTotal()}, väntat ${startReviewTotal})`);
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
    await visit(p, `/journal/${importedTripId(2)}/`, "mork-resa-importerad");
    const gap = (await api(p, "GET", "/trips?page_size=500")).items.find((t) => t.collection_gap);
    if (gap) await visit(p, `/journal/${gap.id}/`, "mork-resa-uppehall");
    await visit(p, "/export/?manad=2026-10", "mork-export-oktober");
    await visit(p, "/settings/?sektion=sammanslagning", "mork-sammanslagning");
    await visit(p, "/settings/?sektion=forslag", "mork-forslag");
    await visit(p, "/settings/?sektion=lunch", "mork-lunch");
    await visit(p, `/journal/${CALENDAR_TRIP}/`, "mork-resa-ur-kalendern");
    await visit(p, "/settings/?sektion=om", "mork-om-drivelog");
    await visit(p, "/settings/?sektion=notiser", "mork-notiser");
    await visit(p, "/settings/?sektion=ersattning", "mork-ersattning-logga");
    await p.getByTestId("export-logo-input").setInputFiles({ name: "logga.png", mimeType: "image/png", buffer: PNG });
    await p.getByTestId("export-logo-preview").waitFor({ timeout: 8000 });
    await p.screenshot({ path: path.join(SHOTS, "mork-foretagslogga.png"), fullPage: true });
    const estDark = (await api(p, "GET", "/trips?page_size=500")).items.find((t) => t.fuel_quality === "estimated");
    if (estDark) await visit(p, `/journal/${estDark.id}/`, "mork-resa-forbrukning");
    await visit(p, "/review/");
    await p.getByTestId("review-ai-review").click();
    await p.getByTestId("ai-review-intro").waitFor({ timeout: 8000 });
    await p.screenshot({ path: path.join(SHOTS, "mork-ai-genomgang.png"), fullPage: true });
    await p.keyboard.press("Escape");
    for (const [type, name] of [["odometer_mismatch", "mork-granskning-avvikelse"], ["odometer_backwards", "mork-granskning-avvikelse-tva-resor"]]) {
      if (await showAnomaly(p, type)) await p.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
    }
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
    await visit(p, "/export/?manad=2026-10", "mobil-export-oktober");
    const mTimes = p.getByTestId("preview-time-mobile");
    for (let i = 0; i < (await mTimes.count()); i++) {
      if ((await lineCount(mTimes.nth(i))) > 1) {
        fail(`mobil: tiden "${await mTimes.nth(i).innerText()}" bryts`);
        break;
      }
    }
    if (!(await p.getByTestId("preview-address-missing").locator("visible=true").count())) fail("mobil: Adress saknas visas inte");
    for (const [route, name] of [
      [`/journal/${importedTripId(1)}/`, "mobil-resa-importerad"],
      ["/settings/?sektion=sammanslagning", "mobil-sammanslagning"],
      ["/settings/?sektion=forslag", "mobil-forslag"],
      ["/settings/?sektion=lunch", "mobil-lunch"],
      ["/settings/?sektion=kalender", "mobil-kalender"],
      [`/journal/${CALENDAR_TRIP}/`, "mobil-resa-ur-kalendern"],
      ["/settings/?sektion=om", "mobil-om-drivelog"],
      ["/settings/?sektion=notiser", "mobil-notiser"],
      ["/settings/?sektion=ersattning", "mobil-ersattning-logga"],
    ]) {
      await visit(p, route, name);
      const wide = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (wide > 2) fail(`${route}: sidan är ${wide} px för bred i mobilen`);
    }
    for (const [type, name] of [["odometer_mismatch", "mobil-granskning-avvikelse"], ["odometer_backwards", "mobil-granskning-avvikelse-tva-resor"]]) {
      const card = await showAnomaly(p, type);
      if (!card) continue;
      await p.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
      await fitsWidth(p, card, `mobil ${type}`);
      const wide = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (wide > 2) fail(`mobil ${type}: sidan är ${wide} px för bred`);
    }
    const gapTrip = (await api(p, "GET", "/trips?page_size=500")).items.find((t) => t.collection_gap);
    if (gapTrip) {
      await visit(p, `/journal/${gapTrip.id}/`, "mobil-resa-uppehall");
      if (!(await p.getByTestId("collection-gap-note").locator("visible=true").count())) fail("mobil: uppehållet visas inte på resan");
    }
    // Versionen i användarmenyn (sidomenyn visas inte på mobilen).
    await visit(p, "/");
    await p.getByRole("button", { name: "Användarmeny" }).click();
    const mv = p.getByTestId("user-menu-version");
    await mv.waitFor({ timeout: 8000 });
    if (!(await mv.innerText()).includes("DriveLog demo")) fail("mobil: användarmenyn visar inte versionen");
    await p.screenshot({ path: path.join(SHOTS, "mobil-anvandarmeny-version.png") });
    await mv.click();
    await p.waitForURL(/sektion=om/, { timeout: 8000 });
    await p.getByTestId("about-card").waitFor({ timeout: 8000 });
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
