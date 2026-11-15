"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, CircleCheckBig, FileUp, MapPin, Merge, NotebookTabs, MessageCircleQuestion } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { formatNumber, milNumber, plural } from "@/lib/format";
import type { ImportCommitResult } from "@/lib/imports-types";
import { periodLabel } from "./import-labels";

function ReceiptLine({
  icon,
  label,
  value,
  hint,
  href,
  linkLabel,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  hint: string;
  href?: string;
  linkLabel?: string;
}) {
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-surface-2 text-fg-subtle [&_svg]:size-4" aria-hidden>
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm">
          <span className="font-mono font-medium">{formatNumber(value, 0)}</span> {label}
        </p>
        <p className="text-xs text-fg-muted">{hint}</p>
      </div>
      {href && value > 0 ? (
        <Button asChild variant="ghost" size="sm" className="shrink-0">
          <Link href={href}>
            {linkLabel} <ArrowRight aria-hidden />
          </Link>
        </Button>
      ) : null}
    </li>
  );
}

/** Steg 3: kvitto efter POST /imports/{id}/commit. */
export function ReceiptStep({
  result,
  onAgain,
  headingRef,
}: {
  result: ImportCommitResult;
  onAgain: () => void;
  headingRef?: React.Ref<HTMLHeadingElement>;
}) {
  const imported = result.already_committed ? (result.imported ?? result.rows_new ?? 0) : (result.imported ?? 0);
  const autoMerged = result.auto_merged ?? 0;
  const questions = result.merge_questions ?? 0;
  const places = result.locations_created ?? 0;
  const superseded = result.reconciled_api_trips?.superseded ?? 0;

  return (
    <section aria-labelledby="import-receipt-title">
      <Card data-testid="import-receipt">
        <div className="p-5 sm:p-6">
          <div className="flex items-start gap-3">
            <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-ok-soft text-ok-ink">
              <CircleCheckBig className="size-5" aria-hidden />
            </span>
            <div className="min-w-0">
              <h2 id="import-receipt-title" ref={headingRef} tabIndex={-1} className="text-lg font-semibold tracking-tight outline-none">
                {imported > 0 ? `${plural(imported, "resa", "resor")} importerade` : "Importen är klar"}
              </h2>
              <p className="mt-0.5 text-sm text-fg-muted">
                {result.already_committed
                  ? "Importen var redan bekräftad – inget har lagts till två gånger."
                  : imported > 0
                    ? `${periodLabel(result.period_start, result.period_end)} · ${milNumber(result.distance_km)} mil. Resorna finns nu i körjournalen.`
                    : "Inga nya resor tillkom – de fanns redan i körjournalen när importen bekräftades."}
              </p>
            </div>
          </div>

          <ul className="mt-5 divide-y divide-border rounded-lg border border-border">
            <ReceiptLine
              icon={<Merge />}
              label={autoMerged === 1 ? "automatisk sammanslagning" : "automatiska sammanslagningar"}
              value={autoMerged}
              hint="Resor med korta stopp emellan som blev en resa."
            />
            <ReceiptLine
              icon={<MessageCircleQuestion />}
              label={questions === 1 ? "fråga om sammanslagning" : "frågor om sammanslagning"}
              value={questions}
              hint={questions > 0 ? "Väntar på ditt svar i Granskning." : "Inget behöver avgöras."}
              href="/review"
              linkLabel="Granska"
            />
            <ReceiptLine
              icon={<MapPin />}
              label={places === 1 ? "plats" : "platser"}
              value={places}
              hint="Adresser i filerna som kopplats till platser."
              href="/places"
              linkLabel="Visa platser"
            />
          </ul>
          {superseded > 0 ? (
            <p className="mt-3 text-[13px] text-fg-muted">
              {plural(superseded, "resa", "resor")} som hämtats från bilen ersattes av originalresorna ur filen.
            </p>
          ) : null}
        </div>
        <div className="flex flex-col gap-3 border-t border-border px-5 py-4 sm:flex-row sm:items-center sm:px-6">
          <Button asChild variant="primary" size="lg">
            <Link href="/journal" data-testid="import-to-journal">
              <NotebookTabs aria-hidden /> Visa i körjournalen
            </Link>
          </Button>
          <Button variant="ghost" size="lg" onClick={onAgain} data-testid="import-again">
            <FileUp aria-hidden /> Importera fler filer
          </Button>
        </div>
      </Card>
    </section>
  );
}
