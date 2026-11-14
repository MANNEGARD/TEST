/**
 * Röktest för DriveLogs webbgränssnitt (Playwright/Chromium).
 *
 * Förutsätter att backend och frontend kör mot en TOM databas, t.ex. via
 * scripts/e2e.sh (som startar allt och kör detta skript).
 *
 * Miljövariabler:
 *   BASE_URL     standard http://localhost:3000
 *   SAMPLES_DIR  katalog med exempelexporter (scripts/make_sample_exports.py)
 *   SHOTS_DIR    katalog för skärmbilder
 *
 * Misslyckas vid konsolfel, sidfel, oväntade 4xx/5xx-svar eller saknade nyckeltexter.
 */
import { createRequire } from "node:module";
import { mkdirSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.env.BASE_URL ?? "http://localhost:3000";
const SAMPLES = process.env.SAMPLES_DIR;
const SHOTS = process.env.SHOTS_DIR ?? path.resolve("e2e-shots");
const EMAIL = "e2e@example.com";
const PASSWORD = "ett-langt-testlosenord";

if (!SAMPLES) {
  console.error("Ange SAMPLES_DIR (kör scripts/make_sample_exports.py först).");
  process.exit(2);
}
mkdirSync(SHOTS, { recursive: true });

const problems = [];
/** Svar som är förväntade i flödet (t.ex. auth-kontroll innan inloggning). */
const EXPECTED = [{ status: 401, url: /\/api\/v1\/auth\/me$/ }];
// 1×1 transparent PNG – kartplattor hämtas aldrig från internet under testet.
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");

function step(msg) {
  console.log(`• ${msg}`);
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

async function newContext(browser, { mobile = false, theme = "light", storageState } = {}) {
  const ctx = await browser.newContext({
    viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 960 },
    deviceScaleFactor: 1,
    colorScheme: theme,
    isMobile: mobile,
    hasTouch: mobile,
    locale: "sv-SE",
    timezoneId: "Europe/Stockholm",
    storageState,
  });
  // Externa förfrågningar (kartplattor) besvaras lokalt.
  await ctx.route(/^https?:\/\/(?!localhost|127\.0\.0\.1)/, (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG }),
  );
  return ctx;
}

async function expectText(page, text, tag) {
  try {
    await page.getByText(text, { exact: false }).filter({ visible: true }).first().waitFor({ state: "visible", timeout: 15000 });
  } catch {
    problems.push(`[${tag}] saknar texten "${text}"`);
  }
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

const browser = await chromium.launch();
let tripId = null;
let storageState;

try {
  // ------------------------------------------------------------------ första kontot
  {
    const ctx = await newContext(browser);
    const page = await ctx.newPage();
    watch(page, "setup");
    step("Skapar första kontot via /setup");
    await page.goto(`${BASE}/setup`);
    await expectText(page, "Skapa ditt konto", "setup");
    await shot(page, "setup-light");
    await page.fill("#name", "Testförare");
    await page.fill("#email", EMAIL);
    await page.fill("#password", PASSWORD);
    await page.fill("#confirm", PASSWORD);
    await page.getByRole("button", { name: "Skapa konto och fortsätt" }).click();
    await page.waitForURL(`${BASE}/`, { timeout: 20000 });
    await expectText(page, "Översikt", "setup");

    // ---------------------------------------------------------------- import via API
    step("Importerar exempelfiler via API:t");
    const me = await (await page.request.get(`${BASE}/api/v1/auth/me`)).json();
    const files = readdirSync(SAMPLES).filter((f) => /\.(xlsx|csv)$/i.test(f));
    if (!files.length) throw new Error(`Inga exempelfiler i ${SAMPLES}`);
    // En förhandsgranskning per fil (Playwrights multipart tar en fil per fältnamn).
    const previews = [];
    for (const f of files) {
      const res = await page.request.post(`${BASE}/api/v1/imports/preview`, {
        headers: { "X-CSRF-Token": me.csrf_token },
        multipart: { files: { name: f, mimeType: "application/octet-stream", buffer: readFileSync(path.join(SAMPLES, f)) } },
      });
      if (res.status() !== 201) throw new Error(`Förhandsgranskning misslyckades (${res.status()}): ${await res.text()}`);
      previews.push(await res.json());
    }
    for (const b of previews) {
      const res = await page.request.post(`${BASE}/api/v1/imports/${b.id}/commit`, {
        headers: { "X-CSRF-Token": me.csrf_token, "Content-Type": "application/json" },
        data: {},
      });
      if (!res.ok()) throw new Error(`Bekräftelse misslyckades (${res.status()}): ${await res.text()}`);
    }
    storageState = await ctx.storageState();
    await ctx.close();
  }

  // ------------------------------------------------------------------ desktop, ljust
  {
    const ctx = await newContext(browser, { storageState });
    const page = await ctx.newPage();
    watch(page, "desktop");

    step("Översikt");
    await page.goto(`${BASE}/`);
    await expectText(page, "Tjänstekörning", "översikt");
    await setPeriodYear(page);
    await expectText(page, "Vanligaste destinationerna", "översikt");
    await expectText(page, "Tjänstekörning över tid", "översikt");
    await shot(page, "overview-light");

    step("Körjournal");
    await page.goto(`${BASE}/journal`);
    await expectText(page, "Körjournal", "journal");
    const firstRow = page.getByTestId("trip-row").first();
    await firstRow.waitFor({ timeout: 15000 });
    await expectText(page, "Summa för", "journal");
    await shot(page, "journal-light");

    step("Byter kategori med ett klick");
    const patch = page.waitForResponse((r) => r.request().method() === "PATCH" && /\/api\/v1\/trips\//.test(r.url()));
    await firstRow.getByRole("radio", { name: "Privat" }).click();
    const patchRes = await patch;
    if (patchRes.status() !== 200) problems.push(`[journal] kategoribyte gav ${patchRes.status()}`);
    await expectText(page, "Kategori: Privat", "journal");

    step("Resedetalj");
    await firstRow.getByRole("link").first().click();
    await page.waitForURL(/\/journal\/[0-9a-f-]{36}$/);
    tripId = page.url().split("/").pop();
    await expectText(page, "Klassning", "detalj");
    await expectText(page, "Färdväg", "detalj");
    await expectText(page, "Underlag och detaljer", "detalj");
    await shot(page, "trip-light");

    step("Granskning och sammanslagningsfråga");
    await page.goto(`${BASE}/review`);
    await expectText(page, "Granskning", "granskning");
    await page.getByTestId("review-current").waitFor({ timeout: 15000 });
    await shot(page, "review-light");
    const answer = page.waitForResponse((r) => /\/merge-questions\/.+\/answer$/.test(r.url()));
    await page.getByTestId("merge-yes").click();
    const ansRes = await answer;
    if (ansRes.status() !== 200) problems.push(`[granskning] svar på sammanslagning gav ${ansRes.status()}`);
    await expectText(page, "slogs samman", "granskning");

    step("Global sökning (Ctrl+K)");
    await page.keyboard.press("Control+k");
    await page.getByRole("textbox", { name: "Sökterm" }).fill("Storstad");
    await page.keyboard.press("Enter");
    await page.waitForURL(/\/journal\?q=Storstad/);
    await page.getByTestId("trip-row").first().waitFor({ timeout: 15000 });

    step("Växlar tema");
    await page.getByTestId("theme-toggle").click();
    await page.getByRole("menuitemradio", { name: "Mörkt" }).click();
    const theme = await page.evaluate(() => document.documentElement.dataset.theme);
    if (theme !== "dark") problems.push(`[tema] förväntade data-theme=dark, fick ${theme}`);
    await ctx.close();
  }

  // ------------------------------------------------------------------ skärmbilder: mörkt + mobil
  for (const variant of [
    { mobile: false, theme: "dark", suffix: "dark" },
    { mobile: true, theme: "light", suffix: "mobile-light" },
    { mobile: true, theme: "dark", suffix: "mobile-dark" },
  ]) {
    const ctx = await newContext(browser, { ...variant, storageState });
    const page = await ctx.newPage();
    watch(page, variant.suffix);
    step(`Skärmbilder (${variant.suffix})`);
    await page.goto(`${BASE}/`);
    await expectText(page, "Tjänstekörning", variant.suffix);
    await setPeriodYear(page);
    await expectText(page, "Vanligaste destinationerna", variant.suffix);
    await shot(page, `overview-${variant.suffix}`);
    await page.goto(`${BASE}/journal`);
    await page.getByTestId(variant.mobile ? "trip-card" : "trip-row").first().waitFor({ timeout: 15000 });
    await shot(page, `journal-${variant.suffix}`);
    if (tripId) {
      await page.goto(`${BASE}/journal/${tripId}`);
      await expectText(page, "Klassning", variant.suffix);
      await shot(page, `trip-${variant.suffix}`);
    }
    await page.goto(`${BASE}/review`);
    await expectText(page, "Granskning", variant.suffix);
    await shot(page, `review-${variant.suffix}`);
    if (variant.mobile) {
      await page.getByRole("button", { name: "Mer" }).click();
      await expectText(page, "Inställningar", variant.suffix);
      await shot(page, `menu-${variant.suffix}`);
    }
    await ctx.close();
  }

  // ------------------------------------------------------------------ inloggningssidan
  {
    const ctx = await newContext(browser, { theme: "dark" });
    const page = await ctx.newPage();
    watch(page, "login");
    step("Inloggningssidan");
    await page.goto(`${BASE}/login`);
    await expectText(page, "Logga in", "login");
    await shot(page, "login-dark");
    await page.fill("#email", EMAIL);
    await page.fill("#password", "fel-losenord-123");
    const fail = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button", { name: "Logga in" }).click();
    const res = await fail;
    if (res.status() === 401) {
      // Förväntat fel – ta bort det ur listan och kontrollera att texten visas.
      const idx = problems.findIndex((p) => p.includes("401 POST /api/v1/auth/login"));
      if (idx >= 0) problems.splice(idx, 1);
      await expectText(page, "Fel e-postadress eller lösenord", "login");
    } else problems.push(`[login] fel lösenord gav ${res.status()}`);
    await page.fill("#password", PASSWORD);
    await page.getByRole("button", { name: "Logga in" }).click();
    await page.waitForURL(`${BASE}/`, { timeout: 15000 });
    await ctx.close();
  }
} catch (err) {
  problems.push(`Avbrott: ${err?.stack ?? err}`);
} finally {
  await browser.close();
}

if (problems.length) {
  console.error(`\n✗ Röktestet misslyckades (${problems.length} problem):`);
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`\n✓ Röktestet gick igenom. Skärmbilder: ${SHOTS}`);
