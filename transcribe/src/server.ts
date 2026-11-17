// Startpunkt: HTTP-server, webbapp, API och live-WebSocket.
import fastifyCookie from "@fastify/cookie";
import fastifyStatic from "@fastify/static";
import fastifyWebsocket from "@fastify/websocket";
import Fastify from "fastify";
import fs from "node:fs";
import { registerApi } from "./api.js";
import { createInvite, inviteUrl, sameOrigin, userFromRequest } from "./auth.js";
import { config } from "./config.js";
import { db } from "./db.js";
import { deepgramEnabled } from "./engines/deepgram.js";
import { whisperEnabled } from "./engines/whisper.js";
import { initJobs } from "./jobs.js";
import { registerLive } from "./live.js";

const app = Fastify({
  logger: { level: config.logLevel },
  // Bakom DriveLogs/egen Caddy: lita på proxyns X-Forwarded-For (Caddy ersätter klientens).
  trustProxy: true,
  bodyLimit: 8 * 1024 * 1024,
});

await app.register(fastifyCookie);
await app.register(fastifyWebsocket, {
  options: { maxPayload: 256 * 1024, perMessageDeflate: false },
});

const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  // Enstaka style-attribut i webbappen (marginaler); inga inline-<style>-block eller skript.
  "style-src-attr 'unsafe-inline'",
  "img-src 'self' data:",
  "media-src 'self' blob:",
  "connect-src 'self'",
  "worker-src 'self'",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

app.addHook("onRequest", async (req, reply) => {
  req.user = userFromRequest(req);
  reply.header("Content-Security-Policy", CSP);
  reply.header("X-Content-Type-Options", "nosniff");
  reply.header("X-Frame-Options", "DENY");
  reply.header("Referrer-Policy", "same-origin");
  reply.header("Permissions-Policy", "microphone=(self), display-capture=(self), camera=(), geolocation=()");
  const m = req.method;
  if (req.url.startsWith("/api/") && m !== "GET" && m !== "HEAD" && m !== "OPTIONS" && !sameOrigin(req)) {
    return reply.code(403).send({ error: "Otillåten begäran (fel ursprung)." });
  }
  if (req.url.startsWith("/api/")) reply.header("Cache-Control", "no-store");
});

registerApi(app);
registerLive(app);

await app.register(fastifyStatic, {
  root: config.webDir,
  index: ["index.html"],
  setHeaders(res, filePath) {
    // index.html laddas alltid om; övriga filer får cachas kort (versionsparameter i index).
    res.setHeader("Cache-Control", filePath.endsWith(".html") ? "no-cache" : "public, max-age=300");
  },
});

app.setNotFoundHandler((req, reply) => {
  if (req.url.startsWith("/api/")) return reply.code(404).send({ error: "Finns inte." });
  return reply.code(404).type("text/plain").send("Finns inte.");
});

fs.mkdirSync(config.dataDir, { recursive: true });

// Första start: ingen användare finns – skapa en inbjudan för administratören och skriv ut den.
const users = (db.prepare("SELECT COUNT(*) AS n FROM users").get() as { n: number }).n;
if (users === 0) {
  db.prepare("DELETE FROM invites WHERE used_at IS NULL AND created_by IS NULL").run();
  const token = createInvite("admin", "Första administratören", null, 2);
  app.log.warn(`Inget konto finns än. Skapa administratörskontot här (giltig i 2 dygn): ${inviteUrl(token)}`);
}

app.log.info(
  {
    live: deepgramEnabled() ? "Deepgram" : "av",
    slutversion: whisperEnabled() ? `${config.whisperModel} @ ${new URL(config.whisperUrl).host}` : "av",
  },
  "Transkriberingstjänster",
);

initJobs(app.log);

const shutdown = async () => {
  await app.close();
  db.close();
  process.exit(0);
};
process.on("SIGTERM", () => void shutdown());
process.on("SIGINT", () => void shutdown());

await app.listen({ port: config.port, host: config.host });
