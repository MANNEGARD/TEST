"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatDate, formatKwh, formatLiters, formatNumber, formatPer100 } from "@/lib/format";
import type { TimeseriesPoint } from "@/lib/types";
import { ChartTooltipBox, axisProps, gridProps } from "./chart-tooltip";
import { bucketLabel } from "./distance-chart";

/**
 * En serie (bensin ELLER el) – två enheter ritas aldrig på samma axel; bensin och
 * el visas som små multiplar bredvid varandra.
 */
export function EnergyMiniChart({ points, bucket, kind }: { points: TimeseriesPoint[]; bucket: "day" | "week" | "month"; kind: "fuel" | "el" }) {
  const key = kind === "fuel" ? "fuel_liters" : "energy_kwh";
  const color = kind === "fuel" ? "var(--fuel)" : "var(--el)";
  const data = points.map((p) => ({ ...p, x: bucketLabel(p, bucket) }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 4, right: 0, bottom: 0, left: -6 }} barCategoryGap="22%">
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="x" {...axisProps} interval="preserveStartEnd" minTickGap={10} />
        <YAxis {...axisProps} axisLine={false} width={44} tickFormatter={(v: number) => formatNumber(v, 0)} />
        <Tooltip
          cursor={{ fill: "var(--surface-2)" }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const p = payload[0].payload as TimeseriesPoint;
            const title = bucket === "day" ? formatDate(p.bucket) : p.label;
            return kind === "fuel" ? (
              <ChartTooltipBox title={title} rows={[{ label: "Bensin", value: formatLiters(p.fuel_liters), colorVar: "--fuel" }]} footer={p.fuel_per_100km != null ? formatPer100(p.fuel_per_100km, "l") : "Ingen uppmätt förbrukning"} />
            ) : (
              <ChartTooltipBox title={title} rows={[{ label: "El", value: formatKwh(p.energy_kwh), colorVar: "--el" }]} footer={p.energy_per_100km != null ? formatPer100(p.energy_per_100km, "kWh") : "Ingen uppmätt förbrukning"} />
            );
          }}
        />
        <Bar dataKey={key} fill={color} maxBarSize={18} radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
