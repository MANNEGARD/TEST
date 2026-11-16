"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, CircleAlert, CircleCheck, Clock, Info, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatKm, plural } from "@/lib/format";
import type { ExportCheck, ExportChecks, ExportPreview } from "@/lib/exports-types";
import { cn } from "@/lib/utils";
import { inSentence } from "./export-period";

/**
 * Kontroller som inte visas här. Luckor i mätarställningen är privat körning och nämns
 * aldrig; demo, tom period och resor som inte tas med visas på annat sätt på sidan.
 */
const HIDDEN_CHECKS = new Set([
  "demo_data",
  "no_trips",
  "undecided_category",
  "preliminary_classification",
  "open_gaps",
  "ignored_gaps",
  "merged_with_gap",
  "summarized_private",
  "incomplete_period",
  "odometer_reconciliation",
  "import_selection",
  "selection_export",
]);

export function visibleChecks(checks: ExportChecks | undefined): ExportCheck[] {
  return (checks?.checks ?? []).filter((c) => !HIDDEN_CHECKS.has(c.code) && c.severity !== "info");
}

/** Resor som har fel enligt kontrollerna – markeras i förhandsvisningen. */
export function flaggedTripIds(checks: ExportChecks | undefined): Set<string> {
  return new Set(visibleChecks(checks).filter((c) => c.severity === "error").flatMap((c) => c.trip_ids));
}

const SETTINGS_VEHICLE_HREF = "/settings"; // sektionen "Fordon och förare" är standardsektionen

interface Item {
  key: string;
  tone: "action" | "error" | "warning" | "tip";
  text: React.ReactNode;
  action?: { label: string; href: string };
}

function checkAction(c: ExportCheck): Item["action"] {
  if (c.code === "place_address_missing") {
    // Adressen fylls i på platsen (Platser), inte på resan.
    const ids = Array.isArray(c.data.location_ids) ? (c.data.location_ids as string[]) : [];
    return { label: ids.length > 1 ? "Öppna Platser" : "Fyll i adressen", href: ids.length === 1 ? `/places?plats=${ids[0]}` : "/places" };
  }
  if (c.trip_ids.length === 1) return { label: "Öppna resan", href: `/journal/${c.trip_ids[0]}` };
  return { label: "Granska", href: "/review" };
}

export function attentionItems(preview: ExportPreview, checks: ExportChecks | undefined): Item[] {
  const items: Item[] = [];
  const { pending_approval: pending, undecided } = preview.excluded;
  if (pending.count > 0) {
    items.push({
      key: "pending",
      tone: "action",
      text: (
        <>
          {plural(pending.count, "resa väntar", "resor väntar")} på ditt godkännande och tas inte med
          <span className="text-fg-muted"> ({formatKm(pending.km)})</span>
        </>
      ),
      action: { label: "Granska", href: "/review" },
    });
  }
  if (undecided.count > 0) {
    items.push({
      key: "undecided",
      tone: "action",
      text: (
        <>
          {plural(undecided.count, "resa är inte avgjord", "resor är inte avgjorda")} – arbete eller privat?
          <span className="text-fg-muted"> ({formatKm(undecided.km)}, tas inte med)</span>
        </>
      ),
      action: { label: "Granska", href: "/review" },
    });
  }
  for (const c of visibleChecks(checks)) {
    items.push({ key: c.code, tone: c.severity === "error" ? "error" : "warning", text: c.message, action: checkAction(c) });
  }
  if (!preview.header.driver) {
    items.push({ key: "driver", tone: "tip", text: "Förarens namn saknas i sidhuvudet.", action: { label: "Fyll i", href: SETTINGS_VEHICLE_HREF } });
  }
  if (!preview.header.registration_number) {
    items.push({ key: "reg", tone: "tip", text: "Registreringsnummer saknas i sidhuvudet.", action: { label: "Fyll i", href: SETTINGS_VEHICLE_HREF } });
  }
  return items;
}

const ICONS: Record<Item["tone"], React.ReactNode> = {
  action: <Clock aria-hidden />,
  error: <CircleAlert aria-hidden />,
  warning: <Info aria-hidden />,
  tip: <UserRound aria-hidden />,
};

const ICON_TONE: Record<Item["tone"], string> = {
  action: "bg-undecided-soft text-undecided-ink",
  error: "bg-err-soft text-err-ink",
  warning: "bg-surface-2 text-fg-subtle",
  tip: "bg-accent-soft text-accent-ink",
};

const TONE_LABEL: Record<Item["tone"], string> = {
  action: "Att göra",
  error: "Fel",
  warning: "Upplysning",
  tip: "Tips",
};

/** Den lugna raden när inget behöver åtgärdas. */
export function AllClear({ periodLabel }: { periodLabel: string }) {
  return (
    <p role="status" className="flex items-center gap-2 text-[13px] text-fg-muted">
      <CircleCheck className="size-4 shrink-0 text-ok" aria-hidden />
      <span>
        Allt klart för <span className="text-fg">{inSentence(periodLabel)}</span>
      </span>
    </p>
  );
}

/** "Innan du skickar" – bara det som faktiskt behöver uppmärksamhet. */
export function AttentionCard({ items, className }: { items: Item[]; className?: string }) {
  if (!items.length) return null;
  return (
    <section aria-labelledby="export-attention-title" className={cn("rounded-xl border border-border bg-surface p-5 shadow-sm", className)}>
      <h2 id="export-attention-title" className="text-sm font-semibold tracking-tight text-fg">
        Innan du skickar
      </h2>
      <ul className="mt-3 space-y-3.5">
        {items.map((it) => (
          <li key={it.key} className="flex gap-3">
            <span className={cn("mt-px flex size-6 shrink-0 items-center justify-center rounded-full [&_svg]:size-3.5", ICON_TONE[it.tone])}>
              {ICONS[it.tone]}
            </span>
            <div className="min-w-0 flex-1">
              <p className={cn("text-[13px] leading-relaxed", it.tone === "warning" ? "text-fg-muted" : "text-fg")}>
                <span className="sr-only">{TONE_LABEL[it.tone]}: </span>
                {it.text}
              </p>
              {it.action ? (
                <Button asChild variant={it.tone === "action" ? "secondary" : "link"} size={it.tone === "action" ? "xs" : "sm"} className={it.tone === "action" ? "mt-2" : "mt-0.5 text-[13px]"}>
                  <Link href={it.action.href}>
                    {it.action.label} <ArrowRight aria-hidden />
                  </Link>
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
