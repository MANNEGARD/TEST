"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatNumber, plural } from "@/lib/format";
import type { ExportPreview, ExportRow } from "@/lib/exports-types";
import { cn } from "@/lib/utils";
import { amountText, milText, rateText } from "./money";

// Mjukt bindestreck: rubriken får brytas som "Mätar-/ställning" i den smala kolumnen.
const ODO_START = "Mätar\u00ADställning start";
const ODO_END = "Mätar\u00ADställning slut";

const FOOTNOTE = "Endast tjänsteresor redovisas. Övrig körning med bilen är privat och ingår inte i underlaget.";

/** "Kontorsgatan 4\nvia Lagergatan 7" → ["Kontorsgatan 4", "via Lagergatan 7"] */
function splitTo(to: string): { dest: string; via: string | null } {
  const i = to.indexOf("\n");
  if (i >= 0) return { dest: to.slice(0, i), via: to.slice(i + 1) };
  if (to.startsWith("via ")) return { dest: "", via: to };
  return { dest: to, via: null };
}

function Odo({ value }: { value: number | null }) {
  return value == null ? <span className="text-fg-subtle">–</span> : <>{formatNumber(value, 0)}</>;
}

function Purpose({ row, flagged }: { row: ExportRow; flagged: boolean }) {
  if (row.purpose) return <>{row.purpose}</>;
  return <span className={flagged ? "font-medium text-err-ink" : "text-fg-subtle"}>Ärende saknas</span>;
}

/** Underlaget så som det ser ut i Excel: sidhuvud, tabell och summering. */
export function JournalPreview({
  preview,
  flaggedIds,
  busy,
}: {
  preview: ExportPreview;
  /** Resor som har fel enligt kontrollerna (markeras i tabellen). */
  flaggedIds: ReadonlySet<string>;
  busy?: boolean;
}) {
  const h = preview.header;
  const meta: [string, React.ReactNode][] = [
    ["Period", h.period],
    ["Förare", h.driver || <span className="text-fg-subtle">Ej angiven</span>],
    ...(h.employer ? ([["Arbetsgivare", h.employer]] as [string, React.ReactNode][]) : []),
    [
      "Bil",
      <>
        {h.vehicle}
        {h.registration_number ? (
          <>
            {h.vehicle ? ", " : null}
            <span className="font-mono tracking-wide">{h.registration_number}</span>
          </>
        ) : null}
      </>,
    ],
  ];
  const title = h.title.replace(/ – DEMODATA$/, "");

  return (
    <article
      aria-label={`Förhandsvisning av körjournalen för ${h.period}`}
      aria-busy={busy || undefined}
      className={cn(
        "min-w-0 rounded-xl border border-border bg-surface shadow-md transition-opacity",
        busy && "opacity-60",
      )}
    >
      <header className="flex flex-col gap-5 px-5 pt-6 pb-5 sm:px-8 sm:pt-8">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold tracking-tight text-fg">{title}</h2>
          {preview.is_demo ? <Badge tone="warn">Demodata</Badge> : null}
        </div>
        <dl className="flex flex-wrap gap-x-10 gap-y-3 text-sm">
          {meta.map(([k, v]) => (
            <div key={k} className="min-w-0">
              <dt className="text-xs text-fg-muted">{k}</dt>
              <dd className="mt-0.5 text-fg">{v}</dd>
            </div>
          ))}
        </dl>
      </header>

      <DesktopTable rows={preview.rows} flaggedIds={flaggedIds} />
      <MobileList rows={preview.rows} flaggedIds={flaggedIds} />

      <Totals preview={preview} />
      <p className="px-5 pb-6 text-xs leading-relaxed text-fg-muted sm:px-8">{FOOTNOTE}</p>
    </article>
  );
}

function DesktopTable({ rows, flaggedIds }: { rows: ExportRow[]; flaggedIds: ReadonlySet<string> }) {
  const router = useRouter();
  return (
    <div className="hidden px-3 md:block lg:px-4">
      <table className="w-full table-fixed border-collapse text-[13px] leading-snug">
        <caption className="sr-only">Tjänsteresor. Välj en rad för att öppna resan.</caption>
        <colgroup>
          <col className="w-[5.6rem]" />
          <col className="w-[6.1rem]" />
          <col />
          <col />
          <col />
          <col className="w-[5rem]" />
          <col className="w-[5rem]" />
          <col className="w-[3.9rem]" />
        </colgroup>
        <thead>
          <tr className="border-y border-border-strong text-left align-bottom text-xs text-fg-muted">
            <th scope="col" className="py-2.5 pr-2 pl-3 font-medium">Datum</th>
            <th scope="col" className="px-2 py-2.5 font-medium">Tid</th>
            <th scope="col" className="px-2 py-2.5 font-medium">Från</th>
            <th scope="col" className="px-2 py-2.5 font-medium">Till</th>
            <th scope="col" className="px-2 py-2.5 font-medium">Ärende</th>
            <th scope="col" className="px-2 py-2.5 text-right font-medium">{ODO_START}</th>
            <th scope="col" className="px-2 py-2.5 text-right font-medium">{ODO_END}</th>
            <th scope="col" className="py-2.5 pr-3 pl-2 text-right font-medium">Km</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const { dest, via } = splitTo(r.to);
            const flagged = flaggedIds.has(r.trip_id);
            const href = `/journal/${r.trip_id}`;
            return (
              <tr
                key={r.trip_id}
                onClick={(e) => {
                  if ((e.target as HTMLElement).closest("a")) return;
                  router.push(href);
                }}
                className={cn(
                  "group cursor-pointer border-b border-border align-top transition-colors last:border-b-0 hover:bg-surface-2/70",
                  flagged && "bg-err-soft/40",
                )}
              >
                <td className="py-2.5 pr-2 pl-3 whitespace-nowrap">
                  <Link
                    href={href}
                    aria-label={`Öppna resan ${r.date ?? ""} ${r.time}`.trim()}
                    className="rounded-sm text-fg tabular-nums focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                  >
                    {r.date ?? "–"}
                  </Link>
                </td>
                <td className="px-2 py-2.5 text-fg-muted tabular-nums">{r.time || <span className="text-fg-subtle">okänd</span>}</td>
                <td className="px-2 py-2.5 break-words text-fg">{r.from || <span className="text-fg-subtle">–</span>}</td>
                <td className="px-2 py-2.5 break-words text-fg">
                  {dest ? <span className="block">{dest}</span> : null}
                  {via ? <span className="block text-xs text-fg-muted">{via}</span> : null}
                  {!dest && !via ? <span className="text-fg-subtle">–</span> : null}
                </td>
                <td className="px-2 py-2.5 break-words text-fg">
                  <Purpose row={r} flagged={flagged} />
                </td>
                <td className="px-2 py-2.5 text-right font-mono text-xs leading-[1.45rem] whitespace-nowrap text-fg-muted tabular-nums">
                  <Odo value={r.odometer_start} />
                </td>
                <td className="px-2 py-2.5 text-right font-mono text-xs leading-[1.45rem] whitespace-nowrap text-fg-muted tabular-nums">
                  <Odo value={r.odometer_end} />
                </td>
                <td className="py-2.5 pr-3 pl-2 text-right font-mono text-xs leading-[1.45rem] font-medium whitespace-nowrap text-fg tabular-nums">{formatNumber(r.km, 1, 1)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function MobileList({ rows, flaggedIds }: { rows: ExportRow[]; flaggedIds: ReadonlySet<string> }) {
  return (
    <ul className="border-y border-border md:hidden" aria-label="Tjänsteresor">
      {rows.map((r) => {
        const { dest, via } = splitTo(r.to);
        const flagged = flaggedIds.has(r.trip_id);
        return (
          <li key={r.trip_id} className="border-b border-border last:border-b-0">
            <Link
              href={`/journal/${r.trip_id}`}
              className={cn(
                "flex gap-3 px-5 py-3.5 transition-colors hover:bg-surface-2/70 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
                flagged && "bg-err-soft/40",
              )}
            >
              <div className="min-w-0 flex-1">
                <div className="font-mono text-xs text-fg-muted">
                  {r.date ?? "–"}
                  {r.time ? <span className="ml-2">{r.time}</span> : null}
                </div>
                <div className="mt-1 text-sm leading-snug text-fg">
                  {r.from || "–"} <ArrowRight className="mx-0.5 inline size-3.5 text-fg-subtle" aria-label="till" /> {dest || "–"}
                </div>
                {via ? <div className="mt-0.5 text-xs text-fg-muted">{via}</div> : null}
                <div className="mt-1 text-[13px] text-fg-muted">
                  <Purpose row={r} flagged={flagged} />
                </div>
              </div>
              <div className="shrink-0 text-right font-mono text-sm font-medium text-fg tabular-nums">
                {formatNumber(r.km, 1, 1)}
                <span className="ml-0.5 text-xs font-normal text-fg-muted">km</span>
              </div>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function Totals({ preview }: { preview: ExportPreview }) {
  const t = preview.totals;
  return (
    <div className="mx-5 mt-1 mb-5 border-t border-border-strong pt-4 sm:mx-8 md:mx-6">
      <dl className="ml-auto grid max-w-sm grid-cols-[1fr_auto] gap-x-6 gap-y-1.5 text-sm">
        <dt className="text-fg-muted">Summa, {plural(t.trips, "resa", "resor")}</dt>
        <dd className="text-right font-mono text-fg tabular-nums">{formatNumber(t.km, 1, 1)} km</dd>
        <dt className="text-fg-muted">Mil (km ÷ 10)</dt>
        <dd className="text-right font-mono text-fg tabular-nums">{milText(t.mil)}</dd>
        {t.amount != null ? (
          <>
            <dt className="font-semibold text-fg">Ersättning à {rateText(t.rate_per_mil)} kr/mil</dt>
            <dd className="text-right font-mono font-semibold text-fg tabular-nums">{amountText(t.amount)}</dd>
          </>
        ) : null}
      </dl>
    </div>
  );
}
