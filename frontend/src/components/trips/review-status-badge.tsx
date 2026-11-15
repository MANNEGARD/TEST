"use client";

import { BadgeCheck, CircleAlert, FileInput, Lightbulb, Sparkles, Wand2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tooltip } from "@/components/ui/tooltip";
import { APPROVAL_SOURCE_LABELS, isAutoApproval, label, REVIEW_STATUS_LABELS, REVIEW_STATUS_SHORT } from "@/lib/labels";

const CONF = {
  needs_review: { tone: "warn", Icon: CircleAlert, explain: "Resan måste granskas av dig innan den räknas som avgjord." },
  proposed: { tone: "accent", Icon: Sparkles, explain: "Systemet har ett förslag som väntar på ditt godkännande." },
  auto: { tone: "neutral", Icon: Wand2, explain: "Automatiskt klassad med hög säkerhet. Preliminär tills du godkänner." },
  approved: { tone: "ok", Icon: BadgeCheck, explain: "Godkänd av dig." },
  imported: { tone: "ok", Icon: FileInput, explain: "Klassningen kommer från den importerade körjournalen." },
} as const;

/** Skälet till ett automatiskt godkännande i klartext: backendens text, annars etiketten för källan. */
export function approvalReasonText(source: string | null | undefined, reason: string | null | undefined): string {
  return reason?.trim() || label(APPROVAL_SOURCE_LABELS, source, "Godkänd automatiskt");
}

/**
 * Granskningsstatus för en resa. `short` = kort etikett, full text i tooltip.
 *
 * En godkänd resa som automatiken avgjorde (approval_source i AUTO_APPROVAL_SOURCES) får
 * märket "Godkänd automatiskt · {skäl}", och ett antaget kundbesök märket "Antaget" –
 * aldrig "Godkänd av dig".
 */
export function ReviewStatusBadge({
  status,
  short = false,
  approvalSource,
  approvalReason,
}: {
  status: string;
  short?: boolean;
  approvalSource?: string | null;
  approvalReason?: string | null;
}) {
  if (status === "approved" && isAutoApproval(approvalSource)) {
    const reason = approvalReasonText(approvalSource, approvalReason);
    if (approvalSource === "assumed") {
      return (
        <Tooltip
          content={
            <>
              <strong className="font-semibold">Antaget</strong>
              <br />
              {reason}. Räknas som tjänst om du inte säger emot – ändra kategori eller ärende om det inte stämmer.
            </>
          }
        >
          <Badge tone="warn" tabIndex={0} className="cursor-help" aria-label={`Status: Antaget – ${reason}`} data-testid="badge-assumed">
            <Lightbulb aria-hidden />
            Antaget
          </Badge>
        </Tooltip>
      );
    }
    const full = `Godkänd automatiskt · ${reason}`;
    return (
      <Tooltip
        content={
          <>
            <strong className="font-semibold">{full}</strong>
            <br />
            DriveLog godkände resan av skälet ovan. Ändrar du något gäller ditt beslut.
          </>
        }
      >
        <Badge tone="ok" tabIndex={0} className="cursor-help max-w-full" aria-label={`Status: ${full}`} data-testid="badge-auto-approved">
          <Wand2 aria-hidden />
          <span className="truncate">{short ? "Automatisk" : full}</span>
        </Badge>
      </Tooltip>
    );
  }
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
