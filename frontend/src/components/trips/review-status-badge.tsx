"use client";

import { BadgeCheck, CircleAlert, FileInput, Sparkles, Wand2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { label, REVIEW_STATUS_LABELS, REVIEW_STATUS_SHORT } from "@/lib/labels";

const CONF = {
  needs_review: { tone: "warn", Icon: CircleAlert, explain: "Resan måste granskas av dig innan den räknas som avgjord." },
  proposed: { tone: "accent", Icon: Sparkles, explain: "Systemet har ett förslag som väntar på ditt godkännande." },
  auto: { tone: "neutral", Icon: Wand2, explain: "Automatiskt klassad med hög säkerhet. Preliminär tills du godkänner." },
  approved: { tone: "ok", Icon: BadgeCheck, explain: "Godkänd av dig." },
  imported: { tone: "ok", Icon: FileInput, explain: "Klassningen kommer från den importerade körjournalen." },
} as const;

/** Granskningsstatus för en resa. `short` = kort etikett, full text i tooltip. */
export function ReviewStatusBadge({ status, short = false }: { status: string; short?: boolean }) {
  const c = CONF[status as keyof typeof CONF] ?? CONF.needs_review;
  const full = label(REVIEW_STATUS_LABELS, status);
  return (
    <Tooltip content={<><strong className="font-semibold">{full}</strong><br />{c.explain}</>}>
      <Badge tone={c.tone} tabIndex={0} className="cursor-help" aria-label={`Status: ${full}`}>
        <c.Icon aria-hidden />
        {short ? label(REVIEW_STATUS_SHORT, status) : full}
      </Badge>
    </Tooltip>
  );
}
