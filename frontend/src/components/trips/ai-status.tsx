"use client";

import { Bot, FileInput, ListChecks, Sparkles, User } from "lucide-react";
import { Tooltip } from "@/components/ui/tooltip";
import { formatPercent } from "@/lib/format";
import { DECISION_SOURCE_LABELS, label } from "@/lib/labels";
import type { Trip } from "@/lib/types";

/** Vem/vad som avgjorde kategorin (AI, regel, mönster, import, du) + säkerhet. */
export function AiStatus({ trip }: { trip: Trip }) {
  const src = trip.category_source;
  const Icon = src === "ai" ? Sparkles : src === "rule" ? ListChecks : src === "pattern" ? Bot : src === "import" ? FileInput : User;
  const conf = trip.confidence != null && (src === "ai" || src === "rule" || src === "pattern") ? formatPercent(trip.confidence) : null;
  const text = label(DECISION_SOURCE_LABELS, src);
  const explain =
    src === "ai" || src === "rule" || src === "pattern"
      ? `Föreslagen av ${text.toLowerCase()}${conf ? ` med ${conf} säkerhet` : ""}. ${trip.review_status === "approved" ? "Godkänd av dig." : "Preliminär tills du godkänner."}`
      : src === "import"
        ? "Kategorin kommer från den importerade körjournalen."
        : src === "user" || src === "ha"
          ? "Avgjord av dig."
          : "Ännu inte bedömd.";
  return (
    <Tooltip content={explain}>
      <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 text-xs whitespace-nowrap text-fg-muted outline-none focus-visible:outline-2 focus-visible:outline-ring">
        <Icon className="size-3.5 text-fg-subtle" aria-hidden />
        {src === "import" ? "Import" : src === "user" ? "Du" : text}
        {conf ? <span className="num text-fg">{conf}</span> : null}
      </span>
    </Tooltip>
  );
}
