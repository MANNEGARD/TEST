// HTML för inloggningssidan och portalen. Sidorna renderas i edge-funktionen, så portalen
// finns aldrig som en statisk fil som går att nå utan inloggning.

import type { Site } from "./sites.ts";

const ASSET_VERSION = "1";

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function layout(title: string, bodyClass: string, body: string, script: string): string {
  return `<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<title>${esc(title)}</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png">
<link rel="preload" href="/fonts/unbounded-latin.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/portal.css?v=${ASSET_VERSION}">
<script src="/${script}?v=${ASSET_VERSION}" defer></script>
</head>
<body class="${bodyClass}">
${body}
</body>
</html>`;
}

const ICONS: Record<Site["icon"], string> = {
  car: '<path d="M5 17h14M6.5 17v2M17.5 17v2M4 13l1.6-4.6A2 2 0 0 1 7.5 7h9a2 2 0 0 1 1.9 1.4L20 13M4 13h16v4H4z"/><circle cx="7.5" cy="15" r=".6"/><circle cx="16.5" cy="15" r=".6"/>',
  home: '<path d="M4 11l8-6 8 6M6 9.5V19h12V9.5M10 19v-5h4v5"/>',
  chip: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
  code: '<path d="M8.5 8L4.5 12l4 4M15.5 8l4 4-4 4M13.5 5.5l-3 13"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
};

const LOCK =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';

export function loginPage(): string {
  return layout(
    "SMGD · Logga in",
    "page-login",
    `<main class="stage">
  <div class="brand">
    <h1 class="sr-only">SMGD</h1>
    <img class="logo" src="/smgd-logo.svg" alt="SMGD – Volvo V90" width="960" height="320">
    <div class="rule" aria-hidden="true"></div>
  </div>

  <section class="card" aria-labelledby="login-title">
    <h2 id="login-title" class="card-title">${LOCK}<span>Logga in</span></h2>
    <noscript><p class="alert">Inloggningen kräver JavaScript.</p></noscript>
    <form id="login-form" novalidate>
      <input type="text" name="username" value="smgd" autocomplete="username" hidden aria-hidden="true" tabindex="-1">
      <label class="field">
        <span>Lösenord</span>
        <input id="password" name="password" type="password" autocomplete="current-password"
               required maxlength="256" autofocus spellcheck="false">
      </label>
      <label class="field">
        <span>Engångskod <small>från din autentiseringsapp</small></span>
        <input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code"
               maxlength="7" required placeholder="000 000" spellcheck="false">
      </label>
      <p id="login-error" class="alert" role="alert" aria-live="assertive" hidden></p>
      <button id="login-submit" class="btn" type="submit">
        <span class="btn-label">Fortsätt</span>
        <span class="spinner" aria-hidden="true"></span>
      </button>
    </form>
  </section>

  <p class="footnote">Krypterad anslutning · Tvåstegsverifiering</p>
</main>`,
    "login.js",
  );
}

export function portalPage(sites: Site[], expiresAt: number): string {
  const cards = sites
    .map((s) => {
      const host = new URL(s.url).host;
      return `<li>
      <a class="site" href="${esc(s.url)}" rel="noopener noreferrer" data-site="${esc(s.id)}">
        <span class="site-icon"><svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[s.icon]}</svg></span>
        <span class="site-text">
          <span class="site-name">${esc(s.name)}</span>
          <span class="site-desc">${esc(s.description)}</span>
          <span class="site-host">${esc(host)}</span>
        </span>
        <span class="status" data-status="unknown" title="Kontrollerar…"><span class="sr-only">Status okänd</span></span>
        <svg class="arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17L17 7M9 7h8v8"/></svg>
      </a>
    </li>`;
    })
    .join("\n");

  return layout(
    "SMGD · Portal",
    "page-portal",
    `<header class="topbar">
  <img class="logo-small" src="/smgd-logo.svg" alt="SMGD" width="180" height="60">
  <button id="logout" class="btn btn-ghost" type="button">Logga ut</button>
</header>
<main class="portal">
  <div class="portal-head">
    <p class="eyebrow">Välkommen tillbaka</p>
    <h1 class="portal-title">Vart vill du gå?</h1>
  </div>
  <ul class="sites">
${cards}
  </ul>
  <p class="footnote" id="session-info" data-expires="${expiresAt}">Sessionen är aktiv.</p>
</main>`,
    "portal.js",
  );
}
