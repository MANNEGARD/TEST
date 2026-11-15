"use client";

import * as React from "react";
import { ArrowRight, Plus, Trash2, WholeWord } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { useSaveSettings } from "@/lib/queries/settings-page";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { useReportDirty } from "./dirty-context";
import { FormCard, SaveBar, SectionIntro } from "./form-parts";

type Row = { id: number; short: string; full: string };
type AbbrMap = Record<string, string>;

const MAX_SHORT = 20;
const MAX_FULL = 120;

let nextId = 1;
function toRows(map: AbbrMap): Row[] {
  return Object.entries(map).map(([short, full]) => ({ id: nextId++, short, full }));
}

function toMap(rows: Row[]): AbbrMap {
  const map: AbbrMap = {};
  for (const r of rows) {
    const short = r.short.trim();
    const full = r.full.trim();
    if (short || full) map[short] = full;
  }
  return map;
}

function sameMap(a: AbbrMap, b: AbbrMap): boolean {
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => b[k] === a[k]);
}

function readMap(data: SettingsPayload, from: "settings" | "defaults"): AbbrMap {
  const v = data[from].abbreviations?.map;
  return v && typeof v === "object" && !Array.isArray(v) ? (v as AbbrMap) : {};
}

/** Fel per rad: { radens id: { fält: text } }. */
function validate(rows: Row[]): Record<number, { short?: string; full?: string }> {
  const errors: Record<number, { short?: string; full?: string }> = {};
  const seen = new Map<string, number>();
  for (const r of rows) {
    const short = r.short.trim();
    const full = r.full.trim();
    if (!short && !full) continue;
    const e: { short?: string; full?: string } = {};
    if (!short) e.short = "Ange förkortningen.";
    else if (short.length > MAX_SHORT) e.short = `Högst ${MAX_SHORT} tecken.`;
    else if (seen.has(short.toLocaleLowerCase("sv"))) e.short = "Förkortningen finns redan.";
    if (!full) e.full = "Ange vad förkortningen betyder.";
    else if (full.length > MAX_FULL) e.full = `Högst ${MAX_FULL} tecken.`;
    if (short) seen.set(short.toLocaleLowerCase("sv"), r.id);
    if (e.short || e.full) errors[r.id] = e;
  }
  return errors;
}

/** 8. Förkortningar – egna förkortningar i kommentarer, t.ex. JKPG → Jönköping. */
export function AbbreviationsSection({ data }: { data: SettingsPayload }) {
  const server = readMap(data, "settings");
  const defaults = readMap(data, "defaults");
  const mutation = useSaveSettings();
  // Utkastet är null tills användaren ändrar något – då visas alltid serverns senaste värden.
  const [draft, setDraft] = React.useState<Row[] | null>(null);
  const [serverRows, setServerRows] = React.useState<{ source: AbbrMap; rows: Row[] }>(() => ({ source: server, rows: toRows(server) }));
  const [failure, setFailure] = React.useState<string | null>(null);
  if (!sameMap(serverRows.source, server)) setServerRows({ source: server, rows: toRows(server) });

  const rows = draft ?? serverRows.rows;
  const map = toMap(rows);
  const dirty = draft !== null && !sameMap(map, server);
  const errors = validate(rows);
  const firstError = Object.values(errors).map((e) => e.short ?? e.full)[0] ?? null;
  const focusId = React.useRef<number | null>(null);
  useReportDirty("forkortningar", dirty);

  const update = (next: Row[]) => {
    setFailure(null);
    setDraft(next);
  };
  const setCell = (id: number, field: "short" | "full", value: string) => update(rows.map((r) => (r.id === id ? { ...r, [field]: value } : r)));
  const addRow = () => {
    const row = { id: nextId++, short: "", full: "" };
    focusId.current = row.id;
    update([...rows, row]);
  };
  const removeRow = (id: number) => update(rows.filter((r) => r.id !== id));

  React.useEffect(() => {
    if (focusId.current == null) return;
    document.getElementById(`abbr-short-${focusId.current}`)?.focus();
    focusId.current = null;
  });

  const persist = (next: AbbrMap, previous: AbbrMap | null) =>
    mutation.mutate(
      { abbreviations: { map: next } },
      {
        onSuccess: () => {
          setDraft(null);
          if (!previous) return void toast.success("Ändringen är ångrad.");
          toast.success("Förkortningarna är sparade. Förslagen för oavgjorda resor räknas om.", {
            action: { label: "Ångra", onClick: () => persist(previous, null) },
          });
        },
        onError: (err) => {
          setFailure(errorMessage(err));
          toast.error(errorMessage(err));
        },
      },
    );

  const save = () => {
    if (!dirty || firstError || mutation.isPending) return;
    persist(map, server);
  };

  return (
    <section aria-labelledby="settings-forkortningar">
      <SectionIntro id="settings-forkortningar" title="Förkortningar">
        Skriver du förkortningar i syftet, till exempel ”JKPG” för Jönköping? Lägg in dem här så förstår förslagen vad du menar.
      </SectionIntro>

      <FormCard onSubmit={save} aria-label="Förkortningar">
        {rows.length === 0 ? (
          <EmptyState
            compact
            icon={<WholeWord />}
            title="Inga förkortningar"
            description="Lägg till de förkortningar du brukar använda i syften och kommentarer."
            action={
              <Button size="sm" onClick={addRow}>
                <Plus aria-hidden /> Lägg till förkortning
              </Button>
            }
          />
        ) : (
          <div className="px-5 py-4">
            <table className="w-full border-separate border-spacing-y-1.5" aria-label="Egna förkortningar">
              <thead>
                <tr className="text-left text-xs font-medium text-fg-muted">
                  <th scope="col" className="w-[38%] pb-1 font-medium">
                    Förkortning
                  </th>
                  <th scope="col" className="w-6" aria-hidden />
                  <th scope="col" className="pb-1 font-medium">
                    Betyder
                  </th>
                  <th scope="col" className="w-9">
                    <span className="sr-only">Åtgärd</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const e = errors[r.id];
                  const label = r.short.trim() || "ny rad";
                  return (
                    <tr key={r.id} className="align-top">
                      <td className="pr-1">
                        <Input
                          id={`abbr-short-${r.id}`}
                          value={r.short}
                          onChange={(ev) => setCell(r.id, "short", ev.target.value)}
                          aria-label={`Förkortning (${label})`}
                          aria-invalid={e?.short ? true : undefined}
                          maxLength={MAX_SHORT + 10}
                          placeholder="t.ex. GBG"
                          className="font-mono uppercase placeholder:normal-case"
                          autoComplete="off"
                          spellCheck={false}
                          autoCapitalize="characters"
                        />
                        {e?.short ? <p className="mt-1 text-xs font-medium text-err-ink">{e.short}</p> : null}
                      </td>
                      <td className="pt-3 text-center text-fg-subtle">
                        <ArrowRight className="inline size-3.5" aria-hidden />
                      </td>
                      <td className="pl-1">
                        <Input
                          value={r.full}
                          onChange={(ev) => setCell(r.id, "full", ev.target.value)}
                          aria-label={`Betydelse av ${label}`}
                          aria-invalid={e?.full ? true : undefined}
                          maxLength={MAX_FULL + 10}
                          placeholder="t.ex. Göteborg"
                          autoComplete="off"
                        />
                        {e?.full ? <p className="mt-1 text-xs font-medium text-err-ink">{e.full}</p> : null}
                      </td>
                      <td className="pl-1">
                        <Button variant="ghost" size="icon" onClick={() => removeRow(r.id)} aria-label={`Ta bort förkortningen ${label}`}>
                          <Trash2 aria-hidden />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <Button variant="ghost" size="sm" className="mt-1 -ml-2" onClick={addRow}>
              <Plus aria-hidden /> Lägg till rad
            </Button>
          </div>
        )}
        <SaveBar
          dirty={dirty}
          invalid={firstError}
          error={failure}
          saving={mutation.isPending}
          onReset={() => {
            setDraft(null);
            setFailure(null);
          }}
          onDefaults={!sameMap(map, defaults) ? () => update(toRows(defaults)) : undefined}
        />
      </FormCard>
    </section>
  );
}
