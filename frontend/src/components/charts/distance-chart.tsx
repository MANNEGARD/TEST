"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatDate, formatKm, formatMil, formatNumber, monthName } from "@/lib/format";
import type { TimeseriesPoint } from "@/lib/types";
import { ChartTooltipBox, axisProps, gridProps } from "./chart-tooltip";

export function bucketLabel(p: TimeseriesPoint, bucket: "day" | "week" | "month"): string {
  if (bucket === "month") {
    const [y, m] = p.bucket.split("-");
    return `${monthName(Number(m), true)} ${y.slice(2)}`;
  }
  if (bucket === "week") return p.label.split(" ")[0];
  return p.bucket.slice(8, 10).replace(/^0/, "");
}

/**
 * Körsträcka över tid. Tjänstekörning är huvudserien; privat och ej avgjord
 * staplas bara om de finns i perioden.
 */
export function DistanceChart({
  points,
  bucket,
  showPrivate,
  showUndecided,
}: {
  points: TimeseriesPoint[];
  bucket: "day" | "week" | "month";
  showPrivate: boolean;
  showUndecided: boolean;
}) {
  const data = points.map((p) => ({ ...p, x: bucketLabel(p, bucket) }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -12 }} barCategoryGap="22%">
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="x" {...axisProps} interval="preserveStartEnd" minTickGap={8} />
        <YAxis {...axisProps} axisLine={false} width={44} tickFormatter={(v: number) => formatNumber(v / 10, 0)} />
        <Tooltip
          cursor={{ fill: "var(--surface-2)" }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const p = payload[0].payload as TimeseriesPoint & { x: string };
            const title = bucket === "day" ? formatDate(p.bucket) : p.label;
            const rows = [{ label: "Arbete", value: formatMil(p.work_km), colorVar: "--work" }];
            if (showPrivate) rows.push({ label: "Privat", value: formatMil(p.private_km), colorVar: "--private" });
            if (showUndecided) rows.push({ label: "Ej avgjord", value: formatMil(p.undecided_km), colorVar: "--undecided" });
            return <ChartTooltipBox title={title} rows={rows} footer={`${p.trips} resor · ${formatKm(p.total_km)} totalt`} />;
          }}
        />
        <Bar dataKey="work_km" name="Arbete" stackId="km" fill="var(--work)" maxBarSize={24} radius={showPrivate || showUndecided ? 0 : [4, 4, 0, 0]} stroke="var(--surface)" strokeWidth={showPrivate || showUndecided ? 1 : 0} isAnimationActive={false} />
        {showUndecided ? <Bar dataKey="undecided_km" name="Ej avgjord" stackId="km" fill="var(--undecided)" maxBarSize={24} stroke="var(--surface)" strokeWidth={1} radius={showPrivate ? 0 : [4, 4, 0, 0]} isAnimationActive={false} /> : null}
        {showPrivate ? <Bar dataKey="private_km" name="Privat" stackId="km" fill="var(--private)" maxBarSize={24} stroke="var(--surface)" strokeWidth={1} radius={[4, 4, 0, 0]} isAnimationActive={false} /> : null}
      </BarChart>
    </ResponsiveContainer>
  );
}
