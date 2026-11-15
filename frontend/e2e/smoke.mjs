/**
 * Helhetstest för DriveLogs webbgränssnitt (Playwright/Chromium) över alla sidor.
 *
 * Förutsätter att backend och frontend kör mot en TOM databas, t.ex. via
 * scripts/e2e.sh (som startar allt på egna portar och kör detta skript).
 *
 * Flöde: första kontot (/setup) → import via webbgränssnittet → Översikt → Körjournal →
 * Resedetalj → Granskning → Export (förhandsvisning = API = Excel-fil) → Platser →
 * Inställningar (ersättning per mil slår igenom) → Integrationer (demolägen) → Bränsle &
 * energi → mobilvy 390×844 → mörkt tema → utloggning och inloggning med retur.
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
import { mkdirSync, readdirSync, writeFileSync } from "node:fs";
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
const EXPECTED = [{ status: 401, url: /\/api\/v1\/auth\/me$/ }];
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
  // Externa förfrågningar (kartplattor) besvaras lokalt.
  await ctx.route(/^https?:\/\/(?!localhost|127\.0\.0\.1)/, (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG }),
  );
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
        await expectText(mq, "Arbete eller privat?", "snabbvy");
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
