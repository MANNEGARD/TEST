"use client";

import * as React from "react";
import { Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import type { IntegrationStatusCode } from "@/lib/integrations-types";
import { Fact, Facts, StatusBadge, TimeAgo } from "./shared";

export interface IntegrationCardData {
  status: IntegrationStatusCode;
  /** Rubrik för raden med senaste lyckade kontakt, t.ex. "Senast synkad". */
  successLabel: string;
  lastSuccessAt: string | null;
  lastError: string | null;
  lastErrorAt: string | null;
  /** Rubrik + innehåll för raden om behörigheter/kapabiliteter. */
  capabilityLabel: string;
  capability: React.ReactNode;
  /** Snabbåtgärd till höger i sidfoten (t.ex. "Synka nu"). */
  action?: React.ReactNode;
}

/**
 * Översiktskort för en integration: status, senaste lyckade synk, senaste fel och
 * behörigheter. Detaljerna ligger bakom "Konfigurera".
 */
export function IntegrationCard({
  id,
  icon,
  name,
  tagline,
  data,
  isLoading,
  error,
  onRetry,
  onConfigure,
}: {
  id: string;
  icon: React.ReactNode;
  name: string;
  tagline: string;
  data: IntegrationCardData | undefined;
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
  onConfigure: () => void;
}) {
  const headingId = `integration-${id}`;
  return (
    <Card className="flex flex-col" role="region" aria-labelledby={headingId} data-testid={`card-${id}`}>
      <div className="flex items-start gap-3.5 px-5 pt-5">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2 text-fg-muted [&_svg]:size-5" aria-hidden>
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
            <h2 id={headingId} className="text-[15px] font-semibold tracking-tight text-fg">
              {name}
            </h2>
            {data ? <StatusBadge status={data.status} /> : isLoading ? <Skeleton className="h-5 w-28 rounded-full" /> : null}
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-fg-muted">{tagline}</p>
        </div>
      </div>

      <div className="flex-1 px-5 pt-4 pb-5">
        {data ? (
          <Facts>
            <Fact label={data.successLabel}>
              <TimeAgo value={data.lastSuccessAt} />
            </Fact>
            <Fact label="Senaste fel">
              {data.lastError ? (
                <span className="text-err-ink">
                  <span className="line-clamp-2 break-words" title={data.lastError}>
                    {data.lastError}
                  </span>
                  {data.lastErrorAt ? (
                    <span className="mt-0.5 block text-xs opacity-80">
                      <TimeAgo value={data.lastErrorAt} />
                    </span>
                  ) : null}
                </span>
              ) : (
                <span className="text-fg-subtle">Inga</span>
              )}
            </Fact>
            <Fact label={data.capabilityLabel}>{data.capability}</Fact>
          </Facts>
        ) : isLoading ? (
          <div className="space-y-3" role="status" aria-label="Laddar">
            <Skeleton className="h-4 w-3/5" />
            <Skeleton className="h-4 w-2/5" />
            <Skeleton className="h-4 w-4/5" />
            <span className="sr-only">Laddar…</span>
          </div>
        ) : (
          <ErrorState compact error={error} onRetry={onRetry} title="Status kunde inte hämtas" className="py-2" />
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-5 py-3">
        <Button onClick={onConfigure} disabled={!data} data-testid={`configure-${id}`} aria-label={`Konfigurera ${name}`}>
          <Settings2 aria-hidden /> Konfigurera
        </Button>
        {data?.action}
      </div>
    </Card>
  );
}
