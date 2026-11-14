"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatChange, formatMil, formatNumber } from "@/lib/format";
import type { MonthlyComparison } from "@/lib/types";
import { ChartTooltipBox, axisProps, gridProps } from "./chart-tooltip";

/** Tjänstekörning per månad, i år jämfört med föregående år (grupperade staplar). */
export function MonthlyChart({ data }: { data: MonthlyComparison }) {
  const rows = data.months.map((m) => ({ x: m.label, current: m.current.work_km, previous: m.previous.work_km, change: m.total_km_change, raw: m }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: -12 }} barGap={2} barCategoryGap="24%">
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="x" {...axisProps} />
        <YAxis {...axisProps} axisLine={false} width={44} tickFormatter={(v: number) => formatNumber(v / 10, 0)} />
        <Tooltip
          cursor={{ fill: "var(--surface-2)" }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const r = payload[0].payload as (typeof rows)[number];
            return (
              <ChartTooltipBox
                title={r.x}
                rows={[
                  { label: String(data.year), value: formatMil(r.current), colorVar: "--work" },
                  { label: String(data.previous_year), value: formatMil(r.previous), colorVar: "--border-strong" },
                ]}
                footer={r.change != null ? `Total körning ${formatChange(r.change)} mot ${data.previous_year}` : undefined}
              />
            );
          }}
        />
        <Bar dataKey="previous" fill="var(--border-strong)" maxBarSize={14} radius={[4, 4, 0, 0]} isAnimationActive={false} />
        <Bar dataKey="current" fill="var(--work)" maxBarSize={14} radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
