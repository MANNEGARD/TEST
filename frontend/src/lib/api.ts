/**
 * Typad fetch-klient mot DriveLogs REST-API (/api/v1, samma origin via rewrite).
 *
 * Konventioner:
 *  - Alla anrop går via `api.get/post/patch/put/del/upload/download`. Anropa aldrig
 *    `fetch("/api/v1/...")` direkt i komponenter.
 *  - Sessionen är en HttpOnly-kaka. Icke-GET-anrop skickar headern X-CSRF-Token.
 *    Token sätts av auth-flödet (login/setup/me) via `setCsrfToken`.
 *  - Fel kastas som `ApiError` med `detail` (svensk text att visa användaren),
 *    `code` (maskinläsbar, t.ex. "gap_requires_ack") och ev. `data`.
 *  - 401 (ej inloggad) skickar användaren till /login?next=… – utom för anrop som
 *    uttryckligen anger `allow401: true` (t.ex. auth/me vid sidstart).
 *  - 403 csrf_failed: token hämtas på nytt via /auth/me och anropet görs om en gång.
 */

import type { paths } from "./api-types";

export const API_BASE = "/api/v1";

export type QueryValue = string | number | boolean | null | undefined | Array<string | number | boolean>;
export type Query = Record<string, QueryValue>;

export interface ApiErrorBody {
  detail?: string;
  code?: string;
  data?: Record<string, unknown>;
  fields?: { field: string; message: string }[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly data: Record<string, unknown>;
  readonly fields: { field: string; message: string }[];

  constructor(status: number, body: ApiErrorBody) {
    const detail = body.detail || defaultMessage(status);
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code || (status === 0 ? "network_error" : "error");
    this.detail = detail;
    this.data = body.data ?? {};
    this.fields = body.fields ?? [];
  }

  /** Väntetid i sekunder för 429-svar ("Vänta 42 sekunder."), annars null. */
  get retryAfterSeconds(): number | null {
    if (this.status !== 429) return null;
    const m = /(\d+)\s*sekund/.exec(this.detail);
    return m ? Number(m[1]) : null;
  }
}

function defaultMessage(status: number): string {
  if (status === 0) return "Kunde inte nå servern. Kontrollera anslutningen och försök igen.";
  if (status === 401) return "Du är inte inloggad.";
  if (status === 403) return "Du saknar behörighet för åtgärden.";
  if (status === 404) return "Resursen hittades inte.";
  if (status === 429) return "För många försök. Vänta en stund och försök igen.";
  if (status >= 500) return "Ett oväntat fel inträffade på servern.";
  return "Förfrågan kunde inte genomföras.";
}

/** Plockar ut ett visningsbart felmeddelande ur vad som helst som kastats. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error && err.message) return err.message;
  return "Något gick fel.";
}

// ----------------------------------------------------------------------------- CSRF

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

// ----------------------------------------------------------------------------- URL

export function buildUrl(path: string, query?: Query): string {
  const url = path.startsWith("/api/") ? path : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const v of value) params.append(key, String(v));
    } else {
      params.append(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

// ----------------------------------------------------------------------------- kärna

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  /** Rå body (FormData/Blob) – sätter inte Content-Type. */
  rawBody?: BodyInit;
  signal?: AbortSignal;
  /** Skicka inte till /login vid 401 (används av auth-kontrollen). */
  allow401?: boolean;
  headers?: Record<string, string>;
}

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

let redirectingToLogin = false;

function redirectToLogin(): void {
  if (typeof window === "undefined" || redirectingToLogin) return;
  const { pathname, search } = window.location;
  if (pathname.startsWith("/login") || pathname.startsWith("/setup")) return;
  redirectingToLogin = true;
  const next = encodeURIComponent(pathname + search);
  window.location.assign(`/login?next=${next}`);
}

async function refreshCsrf(): Promise<boolean> {
  try {
    const res = await fetch(buildUrl("/auth/me"), { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!res.ok) return false;
    const body = (await res.json()) as { csrf_token?: string };
    if (body.csrf_token) {
      setCsrfToken(body.csrf_token);
      return true;
    }
  } catch {
    /* ignorera */
  }
  return false;
}

async function rawRequest(method: Method, path: string, opts: RequestOptions, retried = false): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  let body: BodyInit | undefined;
  if (opts.rawBody !== undefined) {
    body = opts.rawBody;
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      credentials: "same-origin",
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, {});
  }

  if (res.ok) return res;

  let errBody: ApiErrorBody = {};
  try {
    errBody = (await res.json()) as ApiErrorBody;
  } catch {
    /* inte JSON */
  }
  if (res.status === 403 && errBody.code === "csrf_failed" && !retried && (await refreshCsrf())) {
    return rawRequest(method, path, opts, true);
  }
  if (res.status === 401 && !opts.allow401) redirectToLogin();
  throw new ApiError(res.status, errBody);
}

async function request<T>(method: Method, path: string, opts: RequestOptions = {}): Promise<T> {
  const res = await rawRequest(method, path, opts);
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  if (type.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

// ----------------------------------------------------------------------------- nedladdning

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star) return decodeURIComponent(star[1]);
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : null;
}

/** Hämtar en fil (med kaka) och sparar den via en temporär länk. */
async function download(path: string, opts: RequestOptions & { filename?: string; method?: Method } = {}): Promise<void> {
  const res = await rawRequest(opts.method ?? "GET", path, opts);
  const blob = await res.blob();
  const name = opts.filename ?? filenameFromDisposition(res.headers.get("content-disposition")) ?? "fil";
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

// ----------------------------------------------------------------------------- publikt API

export const api = {
  get: <T>(path: string, opts?: Omit<RequestOptions, "body" | "rawBody">) => request<T>("GET", path, opts),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("POST", path, { ...opts, body: body ?? {} }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("PATCH", path, { ...opts, body: body ?? {} }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("PUT", path, { ...opts, body: body ?? {} }),
  del: <T = void>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, opts),
  /** Multipart-uppladdning. `fields` läggs till som formulärfält, `files` under namnet `fileField`. */
  upload: <T>(
    path: string,
    files: File[] | Blob[],
    opts: RequestOptions & { fileField?: string; fields?: Record<string, string | undefined> } = {},
  ) => {
    const form = new FormData();
    for (const f of files) form.append(opts.fileField ?? "files", f);
    for (const [k, v] of Object.entries(opts.fields ?? {})) if (v !== undefined) form.append(k, v);
    return request<T>("POST", path, { ...opts, rawBody: form });
  },
  download,
};

/** Hjälptyp: JSON-svaret (200/201) för en sökväg i OpenAPI-specifikationen. */
export type ApiResponse<
  P extends keyof paths,
  M extends "get" | "post" | "patch" | "put" | "delete" = "get",
> = paths[P][M] extends { responses: infer R }
  ? R extends { 200: { content: { "application/json": infer B } } }
    ? B
    : R extends { 201: { content: { "application/json": infer B } } }
      ? B
      : never
  : never;
