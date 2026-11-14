"use client";

import * as React from "react";
import { BarChart3, Table2 } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export interface LegendItem {
  label: string;
  colorVar: string;
}

/**
 * Ram för diagram: rubrik, förklaring (legend, alltid för ≥ 2 serier), växel
 * diagram/tabell (tillgänglighet) samt laddnings-, fel- och tomt läge.
 */
export function ChartCard({
  title,
  description,
  legend,
  loading,
  error,
  onRetry,
  empty,
  emptyText = "Inga data för perioden.",
  table,
  children,
  className,
  height = 240,
}: {
  title: string;
  description?: React.ReactNode;
  legend?: LegendItem[];
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  empty?: boolean;
  emptyText?: string;
  table?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  height?: number;
}) {
  const [view, setView] = React.useState<"chart" | "table">("chart");
  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader className="pb-2">
        <div className="min-w-0">
          <CardTitle>{title}</CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
        {table && !empty && !loading && !error ? (
          <button
            type="button"
            onClick={() => setView(view === "chart" ? "table" : "chart")}
            className="inline-flex shrink-0 items-center gap-1 rounded-sm px-1.5 py-1 text-xs text-fg-muted hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-ring"
            aria-label={view === "chart" ? `Visa ${title} som tabell` : `Visa ${title} som diagram`}
          >
            {view === "chart" ? <Table2 className="size-3.5" aria-hidden /> : <BarChart3 className="size-3.5" aria-hidden />}
            {view === "chart" ? "Tabell" : "Diagram"}
          </button>
        ) : null}
      </CardHeader>
      <CardContent className="flex-1">
        {legend && legend.length > 1 && view === "chart" && !empty && !loading && !error ? (
          <ul className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label="Förklaring">
            {legend.map((l) => (
              <li key={l.label} className="inline-flex items-center gap-1.5">
                <span className="size-2.5 rounded-[3px]" style={{ background: `var(${l.colorVar})` }} aria-hidden />
                {l.label}
              </li>
            ))}
          </ul>
        ) : null}
        {loading ? (
          <Skeleton style={{ height }} className="w-full" />
        ) : error ? (
          <ErrorState error={error} onRetry={onRetry} compact />
        ) : empty ? (
          <EmptyState title={emptyText} compact className="py-10" />
        ) : view === "table" && table ? (
          <div className="scrollbar-thin overflow-auto" style={{ maxHeight: height + 40 }}>
            {table}
          </div>
        ) : (
          <div style={{ height }}>{children}</div>
        )}
      </CardContent>
    </Card>
  );
}

/** Enkel datatabell för diagrammens tabellvy. */
export function DataTable({ head, rows }: { head: string[]; rows: (React.ReactNode[])[] }) {
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-border">
          {head.map((h, i) => (
            <th key={h} className={cn("py-1.5 text-xs font-medium text-fg-muted", i === 0 ? "text-left" : "text-right")}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-b border-border last:border-0">
            {r.map((c, j) => (
              <td key={j} className={cn("py-1.5", j === 0 ? "text-left" : "text-right font-mono text-[13px]")}>
                {c}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
