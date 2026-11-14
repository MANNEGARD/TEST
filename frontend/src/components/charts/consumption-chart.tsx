"use client";

import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatDate, formatNumber, formatPer100 } from "@/lib/format";
import type { TimeseriesPoint } from "@/lib/types";
import { ChartTooltipBox, axisProps, gridProps } from "./chart-tooltip";
import { bucketLabel } from "./distance-chart";

/** Förbrukning per 100 km över tid (en serie per diagram, bara uppmätta värden). */
export function ConsumptionChart({ points, bucket, kind }: { points: TimeseriesPoint[]; bucket: "day" | "week" | "month"; kind: "fuel" | "el" }) {
  const key = kind === "fuel" ? "fuel_per_100km" : "energy_per_100km";
  const color = kind === "fuel" ? "var(--fuel)" : "var(--el)";
  const data = points.map((p) => ({ ...p, x: bucketLabel(p, bucket) }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -6 }}>
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="x" {...axisProps} interval="preserveStartEnd" minTickGap={10} />
        <YAxis {...axisProps} axisLine={false} width={44} tickFormatter={(v: number) => formatNumber(v, 0)} domain={[0, "auto"]} />
        <Tooltip
          cursor={{ stroke: "var(--chart-axis)" }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const p = payload[0].payload as TimeseriesPoint;
            const v = kind === "fuel" ? p.fuel_per_100km : p.energy_per_100km;
            return <ChartTooltipBox title={bucket === "day" ? formatDate(p.bucket) : p.label} rows={[{ label: kind === "fuel" ? "Bensin" : "El", value: formatPer100(v, kind === "fuel" ? "l" : "kWh"), colorVar: kind === "fuel" ? "--fuel" : "--el" }]} />;
          }}
        />
        <Line type="monotone" dataKey={key} stroke={color} strokeWidth={2} dot={{ r: 4, fill: color, stroke: "var(--surface)", strokeWidth: 2 }} activeDot={{ r: 5, stroke: "var(--surface)", strokeWidth: 2 }} connectNulls isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
