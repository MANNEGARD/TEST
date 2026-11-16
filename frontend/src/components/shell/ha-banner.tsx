"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Smartphone, Unplug } from "lucide-react";
import { formatRelative } from "@/lib/format";
import type { HaStatus } from "@/lib/integrations-types";
import { useHaStatus } from "@/lib/queries/integrations";

/** Så länge Home Assistant får vara tyst innan webben varnar (byggkontraktet C.5). */
export const HEARTBEAT_WARN_MS = 2 * 60 * 60 * 1000;

export type HaProblem = { kind: "phone_missing" } | { kind: "silent"; since: string } | null;

/**
 * Vad banderollen ska säga, eller null. Bara när Home Assistant används på riktigt (inte
 * demoläget, inte okonfigurerat): ingen telefon vald, eller ett livstecken som är äldre än 2 h.
 */
export function haProblem(s: HaStatus | undefined, now: number): HaProblem {
  if (!s || s.demo || s.status === "not_configured") return null;
  if (s.phone_missing) return { kind: "phone_missing" };
  const at = s.heartbeat?.received_at;
  if (at && !s.heartbeat.fresh) {
    const t = new Date(at).getTime();
    if (Number.isFinite(t) && now - t > HEARTBEAT_WARN_MS) return { kind: "silent", since: at };
  }
  return null;
}

function useNow(stepMs: number): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), stepMs);
    return () => window.clearInterval(id);
  }, [stepMs]);
  return now;
}

/** Banderoll när frågorna inte når telefonen: ingen telefon vald eller Home Assistant har tystnat. */
export function HaBanner() {
  const pathname = usePathname();
  const ha = useHaStatus({ intervalMs: 5 * 60_000 });
  const now = useNow(60_000);
  const problem = haProblem(ha.data, now);
  if (!problem) return null;
  const onIntegrations = pathname?.startsWith("/integrations");

  return (
    <div
      role="status"
      data-testid="ha-banner"
      data-kind={problem.kind}
      className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 border-b border-warn/40 bg-warn-soft px-4 py-2 text-center text-[13px] text-warn-ink"
    >
      {problem.kind === "phone_missing" ? (
        <>
          <span className="inline-flex items-center gap-1.5 font-semibold">
            <Smartphone className="size-4" aria-hidden /> Ingen telefon vald
          </span>
          <span>DriveLog vet inte vilken telefon i Home Assistant frågor och notiser ska skickas till. De ligger kvar tills du väljer.</span>
        </>
      ) : (
        <>
          <span className="inline-flex items-center gap-1.5 font-semibold">
            <Unplug className="size-4" aria-hidden /> Home Assistant hörs inte
          </span>
          <span>Senaste livstecknet kom {formatRelative(problem.since)}. Frågor och notiser väntar tills kontakten är tillbaka.</span>
        </>
      )}
      {onIntegrations ? null : (
        <Link href="/integrations" className="font-semibold underline underline-offset-2 hover:no-underline">
          {problem.kind === "phone_missing" ? "Välj telefon" : "Öppna Integrationer"}
        </Link>
      )}
    </div>
  );
}
