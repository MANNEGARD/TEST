"use client";

/**
 * Formulärlogik för en inställningssektion.
 *
 * Utkastet består bara av det användaren ändrat ("overrides") ovanpå serverns värden.
 * När något sparas uppdateras cachen, utkastet sammanfaller med servern och sektionen
 * är inte längre "ändrad" – utan synkronisering i effekter.
 *
 * Fält adresseras som "sektion.nyckel" (t.ex. "merge.auto_max_minutes"). Gränsvärdena
 * i fältbeskrivningarna speglar backend/app/services/settings.py (_BOUNDS, _CHOICES).
 */

import * as React from "react";
import { toast } from "sonner";
import { errorMessage } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries/settings-page";
import type { SettingsChanges, SettingsPayload } from "@/lib/settings-page-types";

export type FieldSpec =
  | { type: "bool" }
  | {
      type: "number";
      /** Fältets namn i felmeddelanden, t.ex. "Gränsen för automatisk sammanslagning". */
      name: string;
      min?: number;
      max?: number;
      integer?: boolean;
      /** Högst så många decimaler visas i fältet (standard 2). */
      decimals?: number;
    }
  | { type: "text"; name?: string; maxLength?: number; validate?: (value: string) => string | null }
  | { type: "time"; name: string }
  | { type: "choice"; options: readonly string[] }
  | { type: "json" };

export type FieldSpecs = Record<string, FieldSpec>;
type FormValue = string | boolean | object;

/** Tal → text i ett inmatningsfält: decimalkomma, inga tusentalsmellanrum. */
export function numberToInput(value: number, decimals = 2): string {
  return formatNumber(value, decimals, 0).replace(/\s/g, "");
}

/** Text → tal. Godtar decimalkomma och mellanrum. Tom sträng ger null, skräp ger NaN. */
export function parseNumber(text: string): number | null {
  const cleaned = text.replace(/\s/g, "").replace(",", ".");
  if (!cleaned) return null;
  if (!/^-?\d*\.?\d+$/.test(cleaned)) return NaN;
  return Number(cleaned);
}

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable((value as Record<string, unknown>)[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function split(path: string): [string, string] {
  const i = path.indexOf(".");
  return [path.slice(0, i), path.slice(i + 1)];
}

function lookup(map: SettingsPayload["settings"], path: string): unknown {
  const [section, key] = split(path);
  return map[section]?.[key];
}

function toForm(spec: FieldSpec, value: unknown): FormValue {
  if (spec.type === "number") return typeof value === "number" ? numberToInput(value, spec.decimals) : "";
  if (spec.type === "bool") return value === true;
  if (spec.type === "json") return (value ?? {}) as object;
  return typeof value === "string" ? value : "";
}

function range(spec: Extract<FieldSpec, { type: "number" }>): string {
  const { min, max, name } = spec;
  if (min != null && max != null) return `${name} måste ligga mellan ${formatNumber(min, 2, 0)} och ${formatNumber(max, 2, 0)}.`;
  if (min != null) return `${name} måste vara minst ${formatNumber(min, 2, 0)}.`;
  return `${name} får vara högst ${formatNumber(max, 2, 0)}.`;
}

/** Tolkar ett formulärvärde. Returnerar värdet att skicka till API:t eller ett felmeddelande. */
function parse(spec: FieldSpec, value: FormValue): { value: unknown; error: string | null } {
  switch (spec.type) {
    case "number": {
      const n = parseNumber(String(value));
      if (n === null) return { value: null, error: `${spec.name}: ange ett värde.` };
      if (Number.isNaN(n)) return { value: null, error: `${spec.name} måste vara ett tal.` };
      if (spec.integer && !Number.isInteger(n)) return { value: n, error: `${spec.name} anges i hela tal.` };
      if ((spec.min != null && n < spec.min) || (spec.max != null && n > spec.max)) return { value: n, error: range(spec) };
      return { value: n, error: null };
    }
    case "time":
      return TIME_RE.test(String(value)) ? { value, error: null } : { value, error: `${spec.name} anges som klockslag (TT:MM).` };
    case "text": {
      const text = String(value);
      if (spec.maxLength != null && text.length > spec.maxLength) {
        return { value: text, error: `${spec.name ?? "Texten"} får vara högst ${formatNumber(spec.maxLength, 0)} tecken.` };
      }
      return { value: text, error: spec.validate?.(text) ?? null };
    }
    case "choice":
      return spec.options.includes(String(value)) ? { value, error: null } : { value, error: "Välj ett av alternativen." };
    default:
      return { value, error: null };
  }
}

function same(spec: FieldSpec, a: unknown, b: unknown): boolean {
  if (spec.type === "number") return typeof a === "number" && typeof b === "number" && Math.abs(a - b) < 1e-9;
  if (spec.type === "json") return stable(a) === stable(b);
  return a === b;
}

export interface SettingsForm {
  /** Finns nyckeln i serverns svar? Inställningar som backend inte känner till visas inte. */
  has: (path: string) => boolean;
  text: (path: string) => string;
  bool: (path: string) => boolean;
  json: <T extends object>(path: string) => T;
  /** Aktuellt tal i ett sifferfält, eller null om fältet är tomt/ogiltigt. */
  number: (path: string) => number | null;
  set: (path: string, value: FormValue) => void;
  error: (path: string) => string | undefined;
  /** Första felet (fält i den ordning de deklarerats, därefter korsvalidering). */
  firstError: string | null;
  dirty: boolean;
  /** Tolkade värden för ändrade fält, per sektion – redo för PATCH. */
  changes: SettingsChanges;
  /** Serverns värden för samma fält (för "Ångra" efter sparning). */
  previous: SettingsChanges;
  reset: () => void;
  /** Sant om något fält avviker från standardvärdet. */
  differsFromDefaults: boolean;
  /** Fyller utkastet med standardvärdena (sparas först när användaren trycker Spara). */
  applyDefaults: () => void;
  /** Standardvärdet för ett fält, som det kom från API:t. */
  defaultOf: (path: string) => unknown;
}

/**
 * @param cross  Korsvalidering mellan fält. Får en funktion som ger aktuella tal och
 *               returnerar { fält: felmeddelande }.
 */
export function useSettingsForm(
  data: SettingsPayload,
  specs: FieldSpecs,
  cross?: (num: (path: string) => number | null) => Partial<Record<string, string>>,
): SettingsForm {
  const [overrides, setOverrides] = React.useState<Record<string, FormValue>>({});

  const has = (path: string) => lookup(data.settings, path) !== undefined;
  const current = (path: string): FormValue => {
    if (path in overrides) return overrides[path];
    const spec = specs[path];
    return spec ? toForm(spec, lookup(data.settings, path)) : "";
  };
  const number = (path: string): number | null => {
    const n = parseNumber(String(current(path)));
    return n === null || Number.isNaN(n) ? null : n;
  };

  const errors: Record<string, string> = {};
  const changes: SettingsChanges = {};
  const previous: SettingsChanges = {};
  let differsFromDefaults = false;

  for (const [path, spec] of Object.entries(specs)) {
    if (!has(path)) continue;
    const server = lookup(data.settings, path);
    const parsed = parse(spec, current(path));
    if (parsed.error) errors[path] = parsed.error;
    const changed = path in overrides && (parsed.error !== null || !same(spec, parsed.value, server));
    if (changed) {
      const [section, key] = split(path);
      (changes[section] ??= {})[key] = parsed.value;
      (previous[section] ??= {})[key] = server;
    }
    const fallback = lookup(data.defaults, path);
    if (fallback !== undefined && (parsed.error !== null || !same(spec, parsed.value, fallback))) differsFromDefaults = true;
  }
  if (cross) {
    for (const [path, message] of Object.entries(cross(number))) if (message && !errors[path]) errors[path] = message;
  }

  const order = Object.keys(specs);
  const firstPath = Object.keys(errors).sort((a, b) => order.indexOf(a) - order.indexOf(b))[0];

  return {
    has,
    text: (path) => String(current(path)),
    bool: (path) => current(path) === true,
    json: <T extends object>(path: string) => current(path) as T,
    number,
    set: (path, value) => setOverrides((o) => ({ ...o, [path]: value })),
    error: (path) => errors[path],
    firstError: firstPath ? errors[firstPath] : null,
    dirty: Object.keys(changes).length > 0,
    changes,
    previous,
    reset: () => setOverrides({}),
    differsFromDefaults,
    applyDefaults: () =>
      setOverrides((o) => {
        const next = { ...o };
        for (const [path, spec] of Object.entries(specs)) {
          const fallback = lookup(data.defaults, path);
          if (fallback !== undefined && has(path)) next[path] = toForm(spec, fallback);
        }
        return next;
      }),
    defaultOf: (path) => lookup(data.defaults, path),
  };
}

/**
 * Sparar formulärets ändringar med kvitto. Kvittot har "Ångra" som återställer de
 * tidigare värdena. Backendens felmeddelande (t.ex. 400 invalid_setting) visas både
 * som toast och i sektionens sparrad.
 */
export function useSectionSave(form: SettingsForm, receipt: string) {
  const mutation = useSaveSettings();
  // Felet hör ihop med just de ändringar som inte gick att spara – ändras utkastet försvinner det.
  const [failure, setFailure] = React.useState<{ changes: string; message: string } | null>(null);

  const save = () => {
    if (!form.dirty || form.firstError || mutation.isPending) return;
    const { changes, previous } = form;
    setFailure(null);
    mutation.mutate(changes, {
      onSuccess: () => {
        form.reset();
        toast.success(receipt, {
          action: {
            label: "Ångra",
            onClick: () =>
              mutation.mutate(previous, {
                onSuccess: () => toast.success("Ändringen är ångrad."),
                onError: (err) => toast.error(errorMessage(err)),
              }),
          },
        });
      },
      onError: (err) => {
        setFailure({ changes: stable(changes), message: errorMessage(err) });
        toast.error(errorMessage(err));
      },
    });
  };

  return {
    save,
    saving: mutation.isPending,
    /** Backendens felmeddelande från senaste sparförsöket. */
    error: failure && failure.changes === stable(form.changes) ? failure.message : null,
  };
}
