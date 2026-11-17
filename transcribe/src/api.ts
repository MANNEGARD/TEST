// REST-API för webbappen.
import fs from "node:fs";
import path from "node:path";
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import {
  clearFailures,
  createInvite,
  createSession,
  destroySession,
  dummyPasswordHash,
  hashPassword,
  inviteUrl,
  isBlocked,
  randomToken,
  registerFailure,
  requireAdmin,
  requireUser,
  sha256,
  validateEmail,
  validatePassword,
  verifyPassword,
} from "./auth.js";
import { config } from "./config.js";
import { audioDir, db, now, parseJson, reindex, type Role, type Segment, type TranscriptRow, type UserRow } from "./db.js";
import { deepgramEnabled } from "./engines/deepgram.js";
import { whisperEnabled } from "./engines/whisper.js";
import { enqueue, queuePosition } from "./jobs.js";
import {
  ensureWritable,
  deleteKey,
  isKeyName,
  KEY_NAMES,
  KeyStoreError,
  serviceLabel,
  setKey,
  status as keyStatus,
  testKey,
  validateKey,
  type KeyName,
} from "./keys.js";
import { isLive } from "./live.js";
import { toTxt } from "./merge.js";

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function str(v: unknown, field: string, max: number, opts: { optional?: boolean; allowEmpty?: boolean } = {}): string {
  if (v === undefined || v === null) {
    if (opts.optional) return "";
    throw new HttpError(400, `${field} saknas.`);
  }
  if (typeof v !== "string") throw new HttpError(400, `${field} har fel format.`);
  const s = v.trim();
  if (!s && !opts.allowEmpty && !opts.optional) throw new HttpError(400, `${field} får inte vara tomt.`);
  if (s.length > max) throw new HttpError(400, `${field} är för långt (max ${max} tecken).`);
  return s;
}

function body(req: FastifyRequest): Record<string, unknown> {
  const b = req.body;
  return b && typeof b === "object" && !Array.isArray(b) ? (b as Record<string, unknown>) : {};
}

function ownTranscript(req: FastifyRequest, id: string): TranscriptRow {
  const row = db.prepare("SELECT * FROM transcripts WHERE id = ?").get(id) as TranscriptRow | undefined;
  if (!row || row.user_id !== req.user!.id) throw new HttpError(404, "Transkriberingen finns inte.");
  return row;
}

function ownFolder(req: FastifyRequest, id: unknown): number | null {
  if (id === null || id === undefined || id === "") return null;
  const n = Number(id);
  const row = db.prepare("SELECT id FROM folders WHERE id = ? AND user_id = ?").get(n, req.user!.id);
  if (!row) throw new HttpError(400, "Mappen finns inte.");
  return n;
}

function cleanTags(v: unknown): string[] {
  if (!Array.isArray(v)) throw new HttpError(400, "Taggar har fel format.");
  const tags = new Set<string>();
  for (const t of v) {
    if (typeof t !== "string") continue;
    const s = t.trim().toLowerCase().replace(/\s+/g, " ").slice(0, 40);
    if (s) tags.add(s);
  }
  if (tags.size > 30) throw new HttpError(400, "Högst 30 taggar.");
  return [...tags];
}

function cleanSegments(v: unknown): Segment[] {
  if (!Array.isArray(v) || v.length > 20000) throw new HttpError(400, "Texten har fel format.");
  return v.map((s) => {
    const o = (s ?? {}) as Record<string, unknown>;
    const start = Number(o.start);
    const end = Number(o.end);
    if (!Number.isFinite(start) || !Number.isFinite(end)) throw new HttpError(400, "Ogiltig tidsstämpel.");
    const speaker = String(o.speaker ?? "0").slice(0, 10);
    if (!/^\d{1,3}$/.test(speaker)) throw new HttpError(400, "Ogiltig talare.");
    return { speaker, start, end, text: String(o.text ?? "").slice(0, 20000) };
  });
}

function cleanSpeakers(v: unknown): Record<string, string> {
  if (!v || typeof v !== "object" || Array.isArray(v)) throw new HttpError(400, "Talare har fel format.");
  const out: Record<string, string> = {};
  for (const [k, name] of Object.entries(v as Record<string, unknown>)) {
    if (!/^\d{1,3}$/.test(k) || typeof name !== "string") continue;
    const n = name.trim().slice(0, 60);
    if (n) out[k] = n;
  }
  return out;
}

function summary(row: TranscriptRow) {
  return {
    id: row.id,
    title: row.title,
    status: row.status,
    statusDetail: row.status_detail,
    folderId: row.folder_id,
    tags: parseJson<string[]>(row.tags, []),
    durationSec: row.duration_sec,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    source: row.source,
  };
}

function detail(row: TranscriptRow) {
  const pos = queuePosition(row.id);
  return {
    ...summary(row),
    error: row.error,
    context: row.context,
    hasAudio: row.has_audio === 1,
    speakers: parseJson<Record<string, string>>(row.speakers, {}),
    segments: parseJson<Segment[]>(row.segments, []),
    liveSegments: parseJson<Segment[]>(row.live_segments, []),
    finalEngine: row.final_engine,
    queuePosition: pos,
    live: isLive(row.id),
  };
}

function removeAudio(id: string): void {
  fs.rmSync(audioDir(id), { recursive: true, force: true });
}

async function sendAudio(req: FastifyRequest, reply: FastifyReply, file: string): Promise<FastifyReply> {
  const stat = fs.statSync(file);
  const range = req.headers.range;
  reply.header("Accept-Ranges", "bytes").header("Content-Type", "audio/mp4").header("Cache-Control", "private, max-age=3600");
  if (range) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(range);
    let start = m && m[1] ? Number(m[1]) : 0;
    let end = m && m[2] ? Number(m[2]) : stat.size - 1;
    if (m && !m[1] && m[2]) {
      start = Math.max(0, stat.size - Number(m[2]));
      end = stat.size - 1;
    }
    if (!m || start > end || start >= stat.size) {
      return reply.code(416).header("Content-Range", `bytes */${stat.size}`).send();
    }
    end = Math.min(end, stat.size - 1);
    reply.code(206).header("Content-Range", `bytes ${start}-${end}/${stat.size}`).header("Content-Length", end - start + 1);
    return reply.send(fs.createReadStream(file, { start, end }));
  }
  reply.header("Content-Length", stat.size);
  return reply.send(fs.createReadStream(file));
}

function safeFilename(title: string): string {
  const base = title
    .normalize("NFC")
    .replace(/[^\p{L}\p{N} _.-]+/gu, "")
    .trim()
    .slice(0, 80);
  return base || "transkribering";
}

// Spärr för kontroll och sparning av API-nycklar: högst 10 anrop per minut och administratör.
const keyCalls = new Map<number, number[]>();
const KEY_CALLS_PER_MINUTE = 10;

function keyCallAllowed(userId: number): boolean {
  const t = Date.now();
  const recent = (keyCalls.get(userId) ?? []).filter((x) => x > t - 60_000);
  if (recent.length >= KEY_CALLS_PER_MINUTE) {
    keyCalls.set(userId, recent);
    return false;
  }
  recent.push(t);
  keyCalls.set(userId, recent);
  return true;
}

function keyName(name: string): KeyName {
  if (!isKeyName(name)) throw new HttpError(404, "Det finns ingen sådan nyckel.");
  return name;
}

export function registerApi(app: FastifyInstance): void {
  app.setErrorHandler((err, req, reply) => {
    if (err instanceof HttpError) return reply.code(err.status).send({ error: err.message });
    const status = (err as { statusCode?: number }).statusCode;
    // Felaktig JSON: svara utan tolkens meddelande, som kan citera det som skickades (t.ex. en nyckel).
    const code = (err as { code?: string }).code ?? "";
    if (status === 400 && (err instanceof SyntaxError || code.startsWith("FST_ERR_CTP_"))) {
      return reply.code(400).send({ error: "Begäran har fel format." });
    }
    if (status && status < 500) return reply.code(status).send({ error: err.message });
    req.log.error({ err }, "Oväntat fel");
    return reply.code(500).send({ error: "Något gick fel på servern." });
  });

  app.get("/api/health", async () => ({ ok: true, version: config.version }));

  // --- Konto ---------------------------------------------------------------------------
  app.get("/api/me", async (req, reply) => {
    if (!req.user) return reply.code(401).send({ error: "Du är inte inloggad." });
    return {
      user: req.user,
      features: { live: deepgramEnabled(), final: whisperEnabled(), diarization: deepgramEnabled() },
      limits: { maxRecordingMinutes: config.maxRecordingMinutes },
    };
  });

  app.post("/api/login", async (req, reply) => {
    const b = body(req);
    const email = str(b.email, "E-post", 254).toLowerCase();
    const password = typeof b.password === "string" ? b.password : "";
    const keys = [`ip:${req.ip}`, `email:${email}`];
    if (isBlocked(keys)) return reply.code(429).send({ error: "För många försök. Vänta en stund och försök igen." });
    const user = db.prepare("SELECT * FROM users WHERE email = ?").get(email) as UserRow | undefined;
    const ok = await verifyPassword(password, user?.password_hash ?? (await dummyPasswordHash()));
    if (!user || !ok || user.disabled) {
      registerFailure(keys);
      return reply.code(401).send({ error: "Fel e-post eller lösenord." });
    }
    clearFailures(keys);
    createSession(reply, user.id, req.headers["user-agent"] ?? "");
    return { ok: true };
  });

  app.post("/api/logout", async (req, reply) => {
    destroySession(req, reply);
    return { ok: true };
  });

  app.post("/api/password", { preHandler: requireUser }, async (req, reply) => {
    const b = body(req);
    const user = db.prepare("SELECT * FROM users WHERE id = ?").get(req.user!.id) as UserRow;
    if (!(await verifyPassword(String(b.current ?? ""), user.password_hash))) {
      return reply.code(400).send({ error: "Nuvarande lösenord stämmer inte." });
    }
    const err = validatePassword(b.next);
    if (err) return reply.code(400).send({ error: err });
    db.prepare("UPDATE users SET password_hash = ? WHERE id = ?").run(await hashPassword(String(b.next)), user.id);
    // Logga ut övriga enheter.
    db.prepare("DELETE FROM sessions WHERE user_id = ?").run(user.id);
    createSession(reply, user.id, req.headers["user-agent"] ?? "");
    return { ok: true };
  });

  app.patch("/api/me", { preHandler: requireUser }, async (req) => {
    const name = str(body(req).name, "Namn", 80, { optional: true });
    db.prepare("UPDATE users SET name = ? WHERE id = ?").run(name, req.user!.id);
    return { ok: true };
  });

  // --- Inbjudningar ----------------------------------------------------------------------
  function findInvite(token: string) {
    return db
      .prepare("SELECT * FROM invites WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?")
      .get(sha256(token), now()) as { token_hash: string; role: Role } | undefined;
  }

  app.get<{ Params: { token: string } }>("/api/invites/:token", async (req, reply) => {
    const inv = findInvite(req.params.token);
    if (!inv) return reply.code(404).send({ error: "Inbjudan är ogiltig, använd eller har gått ut." });
    return { role: inv.role };
  });

  app.post<{ Params: { token: string } }>("/api/invites/:token", async (req, reply) => {
    if (isBlocked([`ip:${req.ip}`])) return reply.code(429).send({ error: "För många försök." });
    const inv = findInvite(req.params.token);
    if (!inv) {
      registerFailure([`ip:${req.ip}`]);
      return reply.code(404).send({ error: "Inbjudan är ogiltig, använd eller har gått ut." });
    }
    const b = body(req);
    const email = str(b.email, "E-post", 254).toLowerCase();
    const emailErr = validateEmail(email);
    if (emailErr) return reply.code(400).send({ error: emailErr });
    const pwErr = validatePassword(b.password);
    if (pwErr) return reply.code(400).send({ error: pwErr });
    const name = str(b.name, "Namn", 80, { optional: true });
    if (db.prepare("SELECT 1 FROM users WHERE email = ?").get(email)) {
      return reply.code(409).send({ error: "Det finns redan ett konto med den e-postadressen." });
    }
    const hash = await hashPassword(String(b.password));
    const userId = db.transaction(() => {
      const res = db
        .prepare("UPDATE invites SET used_at = ? WHERE token_hash = ? AND used_at IS NULL")
        .run(now(), inv.token_hash);
      if (res.changes !== 1) throw new HttpError(409, "Inbjudan har redan använts.");
      const id = Number(
        db.prepare("INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, ?)").run(email, name, hash, inv.role)
          .lastInsertRowid,
      );
      db.prepare("UPDATE invites SET used_by = ? WHERE token_hash = ?").run(id, inv.token_hash);
      return id;
    })();
    createSession(reply, userId, req.headers["user-agent"] ?? "");
    return { ok: true };
  });

  // --- Administration ------------------------------------------------------------------
  app.get("/api/admin/users", { preHandler: requireAdmin }, async () => {
    const month = new Date().toISOString().slice(0, 7);
    return db
      .prepare(
        `SELECT u.id, u.email, u.name, u.role, u.disabled, u.created_at AS createdAt,
          (SELECT COUNT(*) FROM transcripts t WHERE t.user_id = u.id) AS transcripts,
          (SELECT COALESCE(SUM(seconds),0) FROM usage g WHERE g.user_id = u.id AND g.month = ? AND g.engine = 'deepgram-live') AS liveSec,
          (SELECT COALESCE(SUM(seconds),0) FROM usage g WHERE g.user_id = u.id AND g.month = ? AND g.engine = 'whisper') AS whisperSec
         FROM users u ORDER BY u.created_at`,
      )
      .all(month, month);
  });

  app.patch<{ Params: { id: string } }>("/api/admin/users/:id", { preHandler: requireAdmin }, async (req, reply) => {
    const id = Number(req.params.id);
    const b = body(req);
    if (id === req.user!.id) return reply.code(400).send({ error: "Du kan inte ändra ditt eget konto här." });
    const user = db.prepare("SELECT id FROM users WHERE id = ?").get(id);
    if (!user) return reply.code(404).send({ error: "Kontot finns inte." });
    if (typeof b.disabled === "boolean") {
      db.prepare("UPDATE users SET disabled = ? WHERE id = ?").run(b.disabled ? 1 : 0, id);
      if (b.disabled) db.prepare("DELETE FROM sessions WHERE user_id = ?").run(id);
    }
    if (b.role === "admin" || b.role === "user") db.prepare("UPDATE users SET role = ? WHERE id = ?").run(b.role, id);
    return { ok: true };
  });

  app.delete<{ Params: { id: string } }>("/api/admin/users/:id", { preHandler: requireAdmin }, async (req, reply) => {
    const id = Number(req.params.id);
    if (id === req.user!.id) return reply.code(400).send({ error: "Du kan inte ta bort ditt eget konto." });
    const ids = db.prepare("SELECT id FROM transcripts WHERE user_id = ?").all(id) as Array<{ id: string }>;
    db.prepare("DELETE FROM users WHERE id = ?").run(id);
    for (const t of ids) {
      db.prepare("DELETE FROM transcripts_fts WHERE id = ?").run(t.id);
      removeAudio(t.id);
    }
    return { ok: true };
  });

  app.post("/api/admin/invites", { preHandler: requireAdmin }, async (req) => {
    const b = body(req);
    const role: Role = b.role === "admin" ? "admin" : "user";
    const note = str(b.note, "Anteckning", 200, { optional: true });
    const token = createInvite(role, note, req.user!.id);
    return { url: inviteUrl(token) };
  });

  app.get("/api/admin/invites", { preHandler: requireAdmin }, async () => {
    return db
      .prepare(
        `SELECT substr(i.token_hash, 1, 16) AS id, i.role, i.note, i.expires_at AS expiresAt, i.used_at AS usedAt,
          i.created_at AS createdAt, u.email AS usedBy
         FROM invites i LEFT JOIN users u ON u.id = i.used_by ORDER BY i.created_at DESC LIMIT 100`,
      )
      .all();
  });

  app.delete<{ Params: { id: string } }>("/api/admin/invites/:id", { preHandler: requireAdmin }, async (req) => {
    const id = req.params.id.replace(/[^0-9a-f]/g, "").slice(0, 16);
    if (id.length === 16) db.prepare("DELETE FROM invites WHERE substr(token_hash, 1, 16) = ? AND used_at IS NULL").run(id);
    return { ok: true };
  });

  app.get("/api/admin/usage", { preHandler: requireAdmin }, async () => {
    return db
      .prepare("SELECT month, engine, SUM(seconds) AS seconds FROM usage GROUP BY month, engine ORDER BY month DESC LIMIT 60")
      .all();
  });

  // --- API-nycklar (Deepgram och Whisper-tjänsten) ----------------------------------------
  // Nyckelvärdet skickas aldrig tillbaka och loggas aldrig; svaren visar bara de sista tecknen.
  app.get("/api/admin/keys", { preHandler: requireAdmin }, async () => {
    return { keys: KEY_NAMES.map((n) => keyStatus(n)) };
  });

  app.put<{ Params: { name: string } }>("/api/admin/keys/:name", { preHandler: requireAdmin }, async (req, reply) => {
    const name = keyName(req.params.name);
    if (!keyCallAllowed(req.user!.id)) return reply.code(429).send({ error: "För många försök. Vänta en minut." });
    const v = validateKey(body(req).key);
    if ("error" in v) throw new HttpError(400, v.error);
    try {
      ensureWritable();
    } catch (e) {
      if (!(e instanceof KeyStoreError)) throw e;
      req.log.error({ key: name }, e.message);
      return reply.code(500).send({ error: e.message });
    }
    const check = await testKey(name, v.value);
    if (check.result === "denied") {
      req.log.info({ key: name, userId: req.user!.id, check: check.result }, "API-nyckeln nekades och sparades inte");
      return reply.code(400).send({ error: `${serviceLabel(name)} godkände inte nyckeln. Den sparades inte.` });
    }
    try {
      setKey(name, v.value, req.user!.id);
    } catch (e) {
      if (!(e instanceof KeyStoreError)) throw e;
      req.log.error({ key: name }, e.message);
      return reply.code(500).send({ error: e.message });
    }
    req.log.info({ key: name, userId: req.user!.id, check: check.result }, "API-nyckel sparad");
    return { key: keyStatus(name), check };
  });

  app.delete<{ Params: { name: string } }>("/api/admin/keys/:name", { preHandler: requireAdmin }, async (req) => {
    const name = keyName(req.params.name);
    deleteKey(name);
    req.log.info({ key: name, userId: req.user!.id }, "API-nyckel borttagen");
    return { key: keyStatus(name) };
  });

  app.post<{ Params: { name: string } }>("/api/admin/keys/:name/test", { preHandler: requireAdmin }, async (req, reply) => {
    const name = keyName(req.params.name);
    if (!keyCallAllowed(req.user!.id)) return reply.code(429).send({ error: "För många försök. Vänta en minut." });
    return { check: await testKey(name) };
  });

  // --- Mappar ----------------------------------------------------------------------------
  app.get("/api/folders", { preHandler: requireUser }, async (req) => {
    return db
      .prepare(
        `SELECT f.id, f.name, (SELECT COUNT(*) FROM transcripts t WHERE t.folder_id = f.id) AS count
         FROM folders f WHERE f.user_id = ? ORDER BY f.name COLLATE NOCASE`,
      )
      .all(req.user!.id);
  });

  app.post("/api/folders", { preHandler: requireUser }, async (req, reply) => {
    const name = str(body(req).name, "Mappnamn", 60);
    try {
      const res = db.prepare("INSERT INTO folders (user_id, name) VALUES (?, ?)").run(req.user!.id, name);
      return { id: Number(res.lastInsertRowid), name };
    } catch {
      return reply.code(409).send({ error: "Det finns redan en mapp med det namnet." });
    }
  });

  app.patch<{ Params: { id: string } }>("/api/folders/:id", { preHandler: requireUser }, async (req, reply) => {
    const id = ownFolder(req, req.params.id);
    const name = str(body(req).name, "Mappnamn", 60);
    try {
      db.prepare("UPDATE folders SET name = ? WHERE id = ?").run(name, id);
    } catch {
      return reply.code(409).send({ error: "Det finns redan en mapp med det namnet." });
    }
    return { ok: true };
  });

  app.delete<{ Params: { id: string } }>("/api/folders/:id", { preHandler: requireUser }, async (req) => {
    const id = ownFolder(req, req.params.id);
    db.prepare("DELETE FROM folders WHERE id = ?").run(id); // transkriberingarna blir "utan mapp"
    return { ok: true };
  });

  app.get("/api/tags", { preHandler: requireUser }, async (req) => {
    return db
      .prepare(
        `SELECT j.value AS tag, COUNT(*) AS count FROM transcripts t, json_each(t.tags) j
         WHERE t.user_id = ? GROUP BY j.value ORDER BY j.value`,
      )
      .all(req.user!.id);
  });

  // --- Transkriberingar ------------------------------------------------------------------
  app.get<{ Querystring: Record<string, string | undefined> }>(
    "/api/transcripts",
    { preHandler: requireUser },
    async (req) => {
      const q = req.query;
      const where = ["t.user_id = @uid"];
      const params: Record<string, unknown> = { uid: req.user!.id };
      if (q.folder === "none") where.push("t.folder_id IS NULL");
      else if (q.folder && q.folder !== "all") {
        where.push("t.folder_id = @folder");
        params.folder = Number(q.folder);
      }
      if (q.tag) {
        where.push("EXISTS (SELECT 1 FROM json_each(t.tags) j WHERE j.value = @tag)");
        params.tag = q.tag;
      }
      let snippetJoin = "";
      let snippetCol = "'' AS snippet";
      const search = (q.q ?? "").trim().slice(0, 200);
      if (search) {
        // Varje ord som prefix ("mötes*"), citerat så att specialtecken inte tolkas.
        const terms = search
          .split(/\s+/)
          .map((w) => w.replace(/"/g, ""))
          .filter(Boolean)
          .map((w) => `"${w}"*`);
        if (terms.length) {
          snippetJoin = "JOIN transcripts_fts f ON f.id = t.id AND transcripts_fts MATCH @match";
          snippetCol = "snippet(transcripts_fts, 2, '«', '»', ' … ', 18) AS snippet";
          params.match = terms.join(" ");
        }
      }
      const sorts: Record<string, string> = {
        created: "t.created_at",
        title: "t.title COLLATE NOCASE",
        duration: "t.duration_sec",
        updated: "t.updated_at",
      };
      const sort = sorts[q.sort ?? "created"] ?? sorts.created!;
      const order = q.order === "asc" ? "ASC" : "DESC";
      const rows = db
        .prepare(
          `SELECT t.*, ${snippetCol} FROM transcripts t ${snippetJoin}
           WHERE ${where.join(" AND ")} ORDER BY ${sort} ${order}, t.created_at DESC LIMIT 500`,
        )
        .all(params) as Array<TranscriptRow & { snippet: string }>;
      return rows.map((r) => ({ ...summary(r), snippet: r.snippet }));
    },
  );

  app.post("/api/transcripts", { preHandler: requireUser }, async (req) => {
    const b = body(req);
    const defaultTitle = `Inspelning ${new Date().toLocaleString("sv-SE", {
      timeZone: "Europe/Stockholm",
      dateStyle: "short",
      timeStyle: "short",
    })}`;
    const title = str(b.title, "Titel", 200, { optional: true }) || defaultTitle;
    const source = b.source === "tab" ? "tab" : "mic";
    const context = str(b.context, "Ordlista", 1000, { optional: true });
    const folderId = ownFolder(req, b.folderId);
    const tags = b.tags === undefined ? [] : cleanTags(b.tags);
    const id = randomToken(12);
    db.prepare(
      `INSERT INTO transcripts (id, user_id, folder_id, title, status, source, context, tags)
       VALUES (?, ?, ?, ?, 'recording', ?, ?, ?)`,
    ).run(id, req.user!.id, folderId, title, source, context, JSON.stringify(tags));
    reindex(id);
    return { id };
  });

  app.get<{ Params: { id: string } }>("/api/transcripts/:id", { preHandler: requireUser }, async (req) => {
    return detail(ownTranscript(req, req.params.id));
  });

  app.patch<{ Params: { id: string } }>("/api/transcripts/:id", { preHandler: requireUser }, async (req) => {
    const row = ownTranscript(req, req.params.id);
    const b = body(req);
    const sets: string[] = [];
    const params: Record<string, unknown> = { id: row.id, now: now() };
    if (b.title !== undefined) {
      sets.push("title = @title");
      params.title = str(b.title, "Titel", 200);
    }
    if (b.folderId !== undefined) {
      sets.push("folder_id = @folder");
      params.folder = ownFolder(req, b.folderId);
    }
    if (b.tags !== undefined) {
      sets.push("tags = @tags");
      params.tags = JSON.stringify(cleanTags(b.tags));
    }
    if (b.speakers !== undefined) {
      sets.push("speakers = @speakers");
      params.speakers = JSON.stringify(cleanSpeakers(b.speakers));
    }
    if (b.context !== undefined) {
      sets.push("context = @context");
      params.context = str(b.context, "Ordlista", 1000, { optional: true });
    }
    if (b.segments !== undefined) {
      if (row.status === "recording" || row.status === "processing") {
        throw new HttpError(409, "Texten kan redigeras när slutversionen är klar.");
      }
      const field = parseJson<Segment[]>(row.segments, []).length > 0 ? "segments" : "live_segments";
      sets.push(`${field} = @segments`);
      params.segments = JSON.stringify(cleanSegments(b.segments));
    }
    if (sets.length) {
      db.prepare(`UPDATE transcripts SET ${sets.join(", ")}, updated_at = @now WHERE id = @id`).run(params);
      reindex(row.id);
    }
    return detail(ownTranscript(req, row.id));
  });

  app.delete<{ Params: { id: string } }>("/api/transcripts/:id", { preHandler: requireUser }, async (req) => {
    const row = ownTranscript(req, req.params.id);
    if (isLive(row.id)) throw new HttpError(409, "Stoppa inspelningen först.");
    if (row.status === "processing" && queuePosition(row.id) === 0) {
      throw new HttpError(409, "Slutversionen bearbetas just nu – vänta tills den är klar.");
    }
    db.prepare("DELETE FROM transcripts WHERE id = ?").run(row.id);
    db.prepare("DELETE FROM transcripts_fts WHERE id = ?").run(row.id);
    removeAudio(row.id);
    return { ok: true };
  });

  app.post<{ Params: { id: string } }>("/api/transcripts/:id/reprocess", { preHandler: requireUser }, async (req) => {
    const row = ownTranscript(req, req.params.id);
    if (row.status === "recording" || row.status === "processing") throw new HttpError(409, "Den bearbetas redan.");
    if (!row.has_audio && !fs.existsSync(path.join(audioDir(row.id), "recording.wav"))) {
      throw new HttpError(409, "Ljudet finns inte kvar.");
    }
    db.prepare("UPDATE transcripts SET status = 'processing', status_detail = 'I kö', error = '', updated_at = ? WHERE id = ?").run(
      now(),
      row.id,
    );
    enqueue(row.id);
    return detail(ownTranscript(req, row.id));
  });

  /** Avslutar en inspelning vars webbläsare försvann (utan att vänta på tidsgränsen). */
  app.post<{ Params: { id: string } }>("/api/transcripts/:id/finish", { preHandler: requireUser }, async (req) => {
    const row = ownTranscript(req, req.params.id);
    if (row.status !== "recording") throw new HttpError(409, "Inspelningen är redan avslutad.");
    if (isLive(row.id)) throw new HttpError(409, "Inspelningen pågår i ett annat fönster.");
    db.prepare("UPDATE transcripts SET status = 'processing', status_detail = 'I kö', updated_at = ? WHERE id = ?").run(
      now(),
      row.id,
    );
    enqueue(row.id);
    return detail(ownTranscript(req, row.id));
  });

  app.get<{ Params: { id: string } }>("/api/transcripts/:id/audio", { preHandler: requireUser }, async (req, reply) => {
    const row = ownTranscript(req, req.params.id);
    const file = path.join(audioDir(row.id), "audio.m4a");
    if (!fs.existsSync(file)) return reply.code(404).send({ error: "Ljudet är inte klart än." });
    return sendAudio(req, reply, file);
  });

  app.get<{ Params: { id: string }; Querystring: Record<string, string | undefined> }>(
    "/api/transcripts/:id/export.txt",
    { preHandler: requireUser },
    async (req, reply) => {
      const row = ownTranscript(req, req.params.id);
      const finalSegs = parseJson<Segment[]>(row.segments, []);
      const useLive = req.query.version === "live" || finalSegs.length === 0;
      const segs = useLive ? parseJson<Segment[]>(row.live_segments, []) : finalSegs;
      const txt = toTxt(
        { title: row.title, createdAt: row.created_at, durationSec: row.duration_sec, draft: useLive },
        segs,
        parseJson<Record<string, string>>(row.speakers, {}),
        { timestamps: req.query.timestamps !== "0", speakers: req.query.speakers !== "0" },
      );
      const name = `${safeFilename(row.title)}.txt`;
      return reply
        .header("Content-Type", "text/plain; charset=utf-8")
        .header(
          "Content-Disposition",
          `attachment; filename="${name.replace(/[^\x20-\x7e]/g, "_")}"; filename*=UTF-8''${encodeURIComponent(name)}`,
        )
        .send(`﻿${txt}`);
    },
  );
}
