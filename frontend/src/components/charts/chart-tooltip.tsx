"use client";

import type { ReactNode } from "react";

export interface TooltipRow {
  label: string;
  value: string;
  colorVar?: string;
}

/** Tooltip-innehåll i designsystemets stil (text i textfärger, färgruta bär identitet). */
export function ChartTooltipBox({ title, rows, footer }: { title: ReactNode; rows: TooltipRow[]; footer?: ReactNode }) {
  return (
    <div className="min-w-40 rounded-md border border-border bg-surface px-3 py-2 text-xs shadow-md">
      <p className="mb-1.5 font-medium text-fg">{title}</p>
      <ul className="space-y-1">
        {rows.map((r) => (
          <li key={r.label} className="flex items-center gap-2">
            {r.colorVar ? <span className="size-2 rounded-[2px]" style={{ background: `var(${r.colorVar})` }} aria-hidden /> : null}
            <span className="text-fg-muted">{r.label}</span>
            <span className="ml-auto pl-3 font-mono text-fg">{r.value}</span>
          </li>
        ))}
      </ul>
      {footer ? <p className="mt-1.5 border-t border-border pt-1.5 text-fg-muted">{footer}</p> : null}
    </div>
  );
}

/** Gemensamma axel-/rutnätsinställningar för Recharts. */
export const axisProps = {
  tick: { fill: "var(--chart-label)", fontSize: 11 },
  tickLine: false,
  axisLine: { stroke: "var(--chart-axis)" },
} as const;

export const gridProps = { stroke: "var(--chart-grid)", vertical: false, strokeDasharray: "" } as const;
