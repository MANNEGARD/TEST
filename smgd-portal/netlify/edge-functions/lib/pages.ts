// HTML för inloggningssidan och portalen. Sidorna renderas i edge-funktionen, så portalen
// finns aldrig som en statisk fil som går att nå utan inloggning.

import type { Site } from "./sites.ts";

const ASSET_VERSION = "4";

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
      <label class="check">
        <input id="remember" name="remember" type="checkbox">
        <span class="check-box" aria-hidden="true"></span>
        <span>Håll mig inloggad <small>på den här enheten</small></span>
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

export function setupPage(): string {
  return layout(
    "SMGD · Installera inloggning",
    "page-login",
    `<main class="stage">
  <div class="brand">
    <h1 class="sr-only">SMGD</h1>
    <img class="logo logo-sm" src="/smgd-logo.svg" alt="SMGD – Volvo V90" width="960" height="320">
  </div>

  <section class="card card-wide" aria-labelledby="setup-title">
    <h2 id="setup-title" class="card-title">${LOCK}<span>Installera inloggning</span></h2>
    <noscript><p class="alert">Installationen kräver JavaScript.</p></noscript>

    <div id="setup-loading" class="muted">Kontrollerar installationslänken…</div>

    <form id="setup-form" novalidate hidden>
      <ol class="steps">
        <li>
          <p class="step-title">Välj lösenord</p>
          <input type="text" name="username" value="smgd" autocomplete="username" hidden aria-hidden="true" tabindex="-1">
          <label class="field">
            <span>Lösenord <small>minst 12 tecken</small></span>
            <input id="password" type="password" autocomplete="new-password" required minlength="12" maxlength="256" spellcheck="false">
          </label>
          <label class="field">
            <span>Upprepa lösenordet</span>
            <input id="password2" type="password" autocomplete="new-password" required minlength="12" maxlength="256" spellcheck="false">
          </label>
        </li>
        <li>
          <p class="step-title">Skanna QR-koden med din autentiseringsapp</p>
          <p class="muted">T.ex. Google Authenticator, Microsoft Authenticator, 1Password eller Bitwarden.</p>
          <div id="qr" class="qr" aria-label="QR-kod för tvåstegsverifiering" role="img"></div>
          <details class="manual">
            <summary>Kan du inte skanna? Skriv in nyckeln</summary>
            <code id="secret" class="secret"></code>
          </details>
        </li>
        <li>
          <p class="step-title">Skriv koden som appen visar</p>
          <label class="field">
            <span>Engångskod</span>
            <input id="code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="7" required placeholder="000 000" spellcheck="false">
          </label>
        </li>
      </ol>
      <p id="setup-error" class="alert" role="alert" aria-live="assertive" hidden></p>
      <button id="setup-submit" class="btn" type="submit">
        <span class="btn-label">Spara och logga in</span>
        <span class="spinner" aria-hidden="true"></span>
      </button>
    </form>
  </section>

  <p class="footnote">Länken fungerar bara en gång</p>
</main>`,
    "setup.js",
  );
}

export function portalPage(sites: Site[], expiresAt: number, remember = false): string {
  const cards = sites
    .map((s) => {
      const host = new URL(s.url).host;
      return `<li>
      <a class="site" href="${esc(s.url)}" rel="noopener noreferrer" data-site="${esc(s.id)}">
        <span class="site-icon"><img src="${esc(s.logo)}" alt="" width="56" height="56" decoding="async"></span>
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
  <img class="wordmark" src="/smgd-wordmark.svg" alt="SMGD" width="331" height="43">
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
  <p class="footnote" id="session-info" data-expires="${expiresAt}" data-remember="${remember ? "1" : "0"}">${remember ? "Du förblir inloggad på den här enheten" : "Sessionen är aktiv."}</p>
</main>`,
    "portal.js",
  );
}
