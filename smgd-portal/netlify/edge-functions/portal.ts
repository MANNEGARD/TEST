// GET /            – inloggningssidan, eller portalen om sessionen är giltig
// POST /api/logout – tar bort sessionskakan
// GET /api/status  – (inloggad) kontrollerar om subdomänerna svarar
import type { Config, Context } from "@netlify/edge-functions";
import { loginPage, portalPage } from "./lib/pages.ts";
import {
  clearSessionCookie,
  createSessionToken,
  REMEMBER_RENEW_AFTER_SECONDS,
  sessionCookie,
  isSameOrigin,
  json,
  readSessionToken,
  SESSION_COOKIE,
  securityHeaders,
} from "./lib/security.ts";
import { SITES } from "./lib/sites.ts";
import { loadAuthConfig } from "./lib/store.ts";

function html(body: string, status = 200): Response {
  return new Response(body, { status, headers: securityHeaders({ "Content-Type": "text/html; charset=utf-8" }) });
}

async function checkSite(url: string): Promise<"up" | "down"> {
  try {
    const res = await fetch(url, { method: "HEAD", redirect: "manual", signal: AbortSignal.timeout(4000) });
    return res.status < 500 ? "up" : "down";
  } catch {
    return "down";
  }
}

export default async (request: Request, context: Context) => {
  const url = new URL(request.url);
  const cfg = await loadAuthConfig();
  const session = cfg
    ? await readSessionToken(context.cookies.get(SESSION_COOKIE), cfg.sessionSecret, cfg.sessionVersion)
    : null;

  if (url.pathname === "/api/logout") {
    if (request.method !== "POST") return json({ error: "Metoden stöds inte." }, 405, { Allow: "POST" });
    if (!isSameOrigin(request)) return json({ error: "Ogiltig begäran." }, 403);
    return json({ ok: true }, 200, { "Set-Cookie": clearSessionCookie(), "Clear-Site-Data": '"cache", "cookies"' });
  }

  if (url.pathname === "/api/status") {
    if (!session) return json({ error: "Inte inloggad." }, 401);
    const results = await Promise.all(SITES.map(async (s) => [s.id, await checkSite(s.url)] as const));
    return json(Object.fromEntries(results));
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Metoden stöds inte.", { status: 405, headers: securityHeaders({ Allow: "GET, HEAD" }) });
  }

  if (session && cfg) {
    // "Håll mig inloggad": skjut fram utgångstiden vid besök, så att inloggningen aldrig tar slut.
    if (session.rem && Date.now() / 1000 - session.iat > REMEMBER_RENEW_AFTER_SECONDS) {
      const renewed = await createSessionToken(cfg.sessionSecret, cfg.sessionVersion, true);
      const res = html(portalPage(SITES, renewed.session.exp, true));
      res.headers.append("Set-Cookie", sessionCookie(renewed.token, renewed.session));
      return res;
    }
    return html(portalPage(SITES, session.exp, session.rem === true));
  }

  // Ogiltig eller utgången kaka: rensa den och visa inloggningen.
  const res = html(loginPage());
  if (context.cookies.get(SESSION_COOKIE)) res.headers.append("Set-Cookie", clearSessionCookie());
  return res;
};

export const config: Config = {
  path: ["/", "/api/logout", "/api/status"],
};
