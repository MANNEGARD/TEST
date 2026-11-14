"use client";

import * as React from "react";
import { Tooltip } from "@/components/ui/tooltip";
import { MISSING } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Datakvalitet – en bärande del av designen:
 *  - FAKTISKT värde: normal text.
 *  - UPPSKATTAT värde: kursiv + "≈" + streckad underlinje + tooltip med förklaring.
 *  - SAKNAT värde: "–" i dämpad färg med tooltip "Uppgift saknas".
 *
 * `quality` tar både förenklade nivåer och backendens koder:
 *   actual   ← verified, measured, exact, reported, user, odometer, export
 *   estimated← estimated, bounded
 *   missing  ← unknown, none (eller value == null)
 */
export type QualityLevel = "actual" | "estimated" | "missing";

const ACTUAL = new Set(["actual", "verified", "measured", "exact", "reported", "user", "odometer", "export"]);
const ESTIMATED = new Set(["estimated", "bounded"]);

export function qualityLevel(quality: string | null | undefined, hasValue = true): QualityLevel {
  if (!hasValue) return "missing";
  if (!quality) return "actual";
  if (ACTUAL.has(quality)) return "actual";
  if (ESTIMATED.has(quality)) return "estimated";
  return "missing";
}

export function QualityValue({
  value,
  quality,
  explanation,
  missingText = "Uppgift saknas",
  className,
  mono,
}: {
  /** Det formaterade värdet. null/undefined/"–" räknas som saknat. */
  value: React.ReactNode;
  quality?: string | null;
  /** Förklaring i tooltip (för uppskattade och saknade värden). */
  explanation?: React.ReactNode;
  missingText?: React.ReactNode;
  className?: string;
  mono?: boolean;
}) {
  const hasValue = value != null && value !== "" && value !== MISSING;
  const level = qualityLevel(quality, hasValue);

  if (level === "missing") {
    return (
      <Tooltip content={explanation ?? missingText}>
        <span
          tabIndex={0}
          className={cn("cursor-help text-fg-subtle outline-none focus-visible:outline-2 focus-visible:outline-ring", className)}
          aria-label={typeof missingText === "string" ? missingText : "Uppgift saknas"}
        >
          {MISSING}
        </span>
      </Tooltip>
    );
  }
  if (level === "estimated") {
    return (
      <Tooltip content={explanation ?? "Uppskattat värde – inte uppmätt av bilen."}>
        <span
          tabIndex={0}
          className={cn("value-estimated cursor-help whitespace-nowrap outline-none focus-visible:outline-2 focus-visible:outline-ring", mono && "font-mono", className)}
        >
          <span aria-hidden>≈</span>
          <span className="sr-only">ungefär </span>
          {value}
        </span>
      </Tooltip>
    );
  }
  return <span className={cn("whitespace-nowrap", mono && "font-mono", className)}>{value}</span>;
}
