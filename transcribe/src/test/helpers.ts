// Hjälp för proven: tillfälliga kataloger, låtsasservrar för Deepgram och Whisper-tjänsten,
// start av den byggda servern som egen process och enkla HTTP-anrop. Allt sker på 127.0.0.1.
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** transcribe/ (proven körs från dist/test/). */
export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

export function tmpDir(prefix = "transcribe-prov-"): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

export async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as net.AddressInfo).port;
      srv.close(() => resolve(port));
    });
  });
}

export type MockMode = "normal" | "503" | "403" | "hang" | "drop" | "redirect";

export interface SeenRequest {
  method: string;
  path: string;
  authorization: string;
  upgrade: boolean;
}

export interface MockService {
  url: string;
  port: number;
  mode: MockMode;
  /** Nycklar som låtsastjänsten godtar. */
  valid: Set<string>;
  /** Godta anrop helt utan nyckel (som en egen Whisper-server utan inloggning). */
  open: boolean;
  seen: SeenRequest[];
  close(): Promise<void>;
}

// Svaren vid fel nyckel är de som de riktiga tjänsterna gav vid en kontroll mot dem (Deepgram
// skickar fler fält än de två som återges här; koden läser ändå bara statuskoden). Vad en giltig
// nyckel ger har inte kunnat prövas mot de riktiga tjänsterna, och koden bygger inte på det.
const DEEPGRAM_401 = { category: "UNAUTHORIZED", message: "Authentication failed." };
const GROQ_401 = { error: { message: "Invalid API Key", type: "invalid_request_error", code: "invalid_api_key" } };

function json(res: http.ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(text) });
  res.end(text);
}

/**
 * Låtsasserver. kind "deepgram": GET /v1/projects (Authorization: Token …), POST /v1/listen och
 * WebSocket /v1/listen. kind "whisper": GET <prefix>/models och POST <prefix>/audio/transcriptions
 * (Authorization: Bearer …). `prefix` gör det möjligt att låta adressen innehålla "groq.com".
 */
export async function startMock(kind: "deepgram" | "whisper", prefix = ""): Promise<MockService> {
  const sockets = new Set<net.Socket>();
  const mock: MockService = {
    url: "",
    port: 0,
    mode: "normal",
    valid: new Set(),
    open: false,
    seen: [],
    close: async () => {
      for (const s of sockets) s.destroy();
      await new Promise<void>((r) => server.close(() => r()));
    },
  };
  const scheme = kind === "deepgram" ? "Token " : "Bearer ";
  const accepted = (auth: string) =>
    (mock.open && auth === "") || (auth.startsWith(scheme) && mock.valid.has(auth.slice(scheme.length)));
  const denied = (res: http.ServerResponse) => json(res, 401, kind === "deepgram" ? DEEPGRAM_401 : GROQ_401);

  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://x");
    const auth = req.headers.authorization ?? "";
    mock.seen.push({ method: req.method ?? "", path: url.pathname, authorization: auth, upgrade: false });
    req.resume();
    req.on("end", () => {
      if (mock.mode === "hang") return;
      if (mock.mode === "drop") return void req.socket.destroy();
      if (mock.mode === "503") return json(res, 503, { error: "otillgänglig" });
      if (mock.mode === "403") return json(res, 403, { error: "förbjuden" });
      if (mock.mode === "redirect") {
        res.writeHead(302, { Location: `${mock.url}/omdirigerad` });
        return void res.end();
      }
      const p = url.pathname.slice(prefix.length);
      if (kind === "deepgram" && req.method === "GET" && p === "/v1/projects") {
        return accepted(auth) ? json(res, 200, { projects: [] }) : denied(res);
      }
      if (kind === "deepgram" && req.method === "POST" && p === "/v1/listen") {
        return accepted(auth) ? json(res, 200, { results: { channels: [{ alternatives: [{ words: [] }] }] } }) : denied(res);
      }
      if (kind === "whisper" && req.method === "GET" && p === "/models") {
        return accepted(auth) ? json(res, 200, { object: "list", data: [] }) : denied(res);
      }
      if (kind === "whisper" && req.method === "POST" && p === "/audio/transcriptions") {
        return accepted(auth) ? json(res, 200, { text: "", segments: [], words: [] }) : denied(res);
      }
      json(res, 404, { error: "finns inte" });
    });
  });
  server.on("connection", (s) => {
    sockets.add(s);
    s.on("close", () => sockets.delete(s));
  });
  // Live-anslutningen: anteckna huvudet och bryt (motsvarar ett nätavbrott).
  server.on("upgrade", (req, socket) => {
    const url = new URL(req.url ?? "/", "http://x");
    mock.seen.push({ method: "GET", path: url.pathname, authorization: req.headers.authorization ?? "", upgrade: true });
    socket.destroy();
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
  mock.port = (server.address() as net.AddressInfo).port;
  mock.url = `http://127.0.0.1:${mock.port}`;
  return mock;
}

export interface HttpResponse {
  status: number;
  headers: http.IncomingHttpHeaders;
  text: string;
  json: any;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  rawBody?: string;
  cookie?: string;
  headers?: Record<string, string>;
  /** Utan X-Requested-With (för prov av ursprungskontrollen). */
  noMarker?: boolean;
}

/** Alla svarstexter som tagits emot, för att kunna söka efter nycklar i dem. */
export const allResponses: string[] = [];

export function request(base: string, rawPath: string, opts: RequestOptions = {}): Promise<HttpResponse> {
  const u = new URL(base);
  const headers: Record<string, string> = { ...(opts.headers ?? {}) };
  if (!opts.noMarker) headers["X-Requested-With"] = "transcribe";
  if (opts.cookie) headers.Cookie = opts.cookie;
  let payload: string | undefined;
  if (opts.rawBody !== undefined) payload = opts.rawBody;
  else if (opts.body !== undefined) payload = JSON.stringify(opts.body);
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = String(Buffer.byteLength(payload));
  }
  return new Promise((resolve, reject) => {
    // node:http skickar sökvägen precis som den skrivs (ingen normalisering av %61 o.d.).
    const req = http.request(
      { host: u.hostname, port: u.port, method: opts.method ?? "GET", path: rawPath, headers },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          allResponses.push(text);
          let parsed: unknown = null;
          try {
            parsed = JSON.parse(text);
          } catch {
            /* inte JSON */
          }
          resolve({ status: res.statusCode ?? 0, headers: res.headers, text, json: parsed });
        });
      },
    );
    req.on("error", reject);
    if (payload !== undefined) req.write(payload);
    req.end();
  });
}

export interface RunningServer {
  url: string;
  port: number;
  dataDir: string;
  env: Record<string, string>;
  output(): string;
  stop(): Promise<void>;
}

/** Miljö utan ärvda nycklar: bara det som anges plus PATH. */
export function cleanEnv(extra: Record<string, string>): Record<string, string> {
  return { PATH: process.env.PATH ?? "/usr/bin:/bin", TZ: "Europe/Stockholm", ...extra };
}

export async function startServer(dataDir: string, extra: Record<string, string>): Promise<RunningServer> {
  const port = await freePort();
  const url = `http://127.0.0.1:${port}`;
  const env = cleanEnv({
    TRANSCRIBE_DATA_DIR: dataDir,
    TRANSCRIBE_WEB_DIR: path.join(ROOT, "web"),
    TRANSCRIBE_PORT: String(port),
    TRANSCRIBE_HOST: "127.0.0.1",
    TRANSCRIBE_PUBLIC_URL: url,
    TRANSCRIBE_LOG_LEVEL: "info",
    ...extra,
  });
  const child: ChildProcess = spawn(process.execPath, [path.join(ROOT, "dist", "server.js")], {
    cwd: ROOT,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let out = "";
  child.stdout!.on("data", (c: Buffer) => (out += c.toString("utf8")));
  child.stderr!.on("data", (c: Buffer) => (out += c.toString("utf8")));
  const exited = new Promise<void>((r) => child.once("exit", () => r()));
  const deadline = Date.now() + 20_000;
  for (;;) {
    if (child.exitCode !== null) throw new Error(`Servern avslutades vid start:\n${out}`);
    try {
      const res = await request(url, "/api/health");
      if (res.status === 200) break;
    } catch {
      /* inte uppe än */
    }
    if (Date.now() > deadline) {
      child.kill("SIGKILL");
      throw new Error(`Servern startade inte:\n${out}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return {
    url,
    port,
    dataDir,
    env,
    output: () => out,
    stop: async () => {
      if (child.exitCode === null) {
        child.kill("SIGTERM");
        const t = setTimeout(() => child.kill("SIGKILL"), 5000);
        await exited;
        clearTimeout(t);
      }
    },
  };
}

export function cookieFrom(res: HttpResponse): string {
  const set = res.headers["set-cookie"] ?? [];
  const c = set.map((s) => s.split(";")[0]!).find((s) => s.startsWith("ts_session="));
  if (!c) throw new Error(`Ingen sessionscookie (status ${res.status}): ${res.text}`);
  return c;
}

let accountNo = 0;

/** Skapar ett konto via en inbjudningstoken och ger sessionscookien. */
export async function acceptInvite(base: string, token: string): Promise<string> {
  accountNo++;
  const res = await request(base, `/api/invites/${token}`, {
    method: "POST",
    body: { email: `prov${accountNo}@example.com`, password: "provlosenord-1234", name: `Prov ${accountNo}` },
  });
  return cookieFrom(res);
}

/** Första administratören, via länken som servern skriver i loggen vid första start. */
export async function firstAdmin(server: RunningServer): Promise<string> {
  const m = /#\/inbjudan\/([A-Za-z0-9_-]+)/.exec(server.output());
  if (!m) throw new Error(`Ingen inbjudan i loggen:\n${server.output()}`);
  return acceptInvite(server.url, m[1]!);
}

/** Ett nytt konto inbjudet av en administratör. */
export async function invitedAccount(base: string, adminCookie: string, role: "admin" | "user"): Promise<string> {
  const res = await request(base, "/api/admin/invites", { method: "POST", cookie: adminCookie, body: { role } });
  const m = /#\/inbjudan\/([A-Za-z0-9_-]+)/.exec(String(res.json?.url ?? ""));
  if (!m) throw new Error(`Ingen inbjudan: ${res.text}`);
  return acceptInvite(base, m[1]!);
}
