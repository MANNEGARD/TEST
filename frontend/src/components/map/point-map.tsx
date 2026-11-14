"use client";

import * as React from "react";
import "maplibre-gl/dist/maplibre-gl.css";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export interface MapPoint {
  lat: number;
  lon: number;
  kind: "start" | "stop" | "end" | "observation" | "position";
  label: string;
  /** Text i popup (HTML escapas). */
  detail?: string;
}

const COLORS = {
  start: "#1e8e3e",
  stop: "#c98200",
  end: "#c93434",
  observation: "#6d7178",
  position: "#2b5c8f",
};

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}

function attributionFor(url: string): string {
  if (url.includes("openstreetmap")) return '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
  return "";
}

/**
 * Karta med kända punkter (MapLibre GL, rasterplattor från användarens inställning).
 * `connect` ritar en STRECKAD linje mellan punkterna i ordning – det är aldrig
 * den faktiskt körda vägen (visa alltid en förklarande text bredvid).
 */
export function PointMap({
  points,
  tilesUrl,
  connect = false,
  className,
  ariaLabel,
}: {
  points: MapPoint[];
  tilesUrl: string;
  connect?: boolean;
  className?: string;
  ariaLabel: string;
}) {
  const ref = React.useRef<HTMLDivElement>(null);
  const { resolved } = useTheme();
  const key = JSON.stringify(points) + tilesUrl + connect;

  React.useEffect(() => {
    let disposed = false;
    let map: import("maplibre-gl").Map | null = null;
    let observer: ResizeObserver | null = null;
    (async () => {
      const ml = await import("maplibre-gl");
      if (disposed || !ref.current) return;
      ml.setWorkerUrl("/vendor/maplibre-gl-worker.mjs");
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      map = new ml.Map({
        container: ref.current,
        style: {
          version: 8,
          sources: {
            tiles: { type: "raster", tiles: [tilesUrl], tileSize: 256, attribution: attributionFor(tilesUrl), maxzoom: 19 },
          },
          layers: [
            {
              id: "tiles",
              type: "raster",
              source: "tiles",
              paint: resolved === "dark" ? { "raster-brightness-max": 0.62, "raster-saturation": -0.55, "raster-contrast": 0.1 } : { "raster-saturation": -0.2 },
            },
          ],
        },
        center: [points[0].lon, points[0].lat],
        zoom: 12,
        attributionControl: { compact: true },
        cooperativeGestures: true,
        fadeDuration: reduced ? 0 : 300,
      });
      observer = new ResizeObserver(() => map?.resize());
      observer.observe(ref.current);
      map.addControl(new ml.NavigationControl({ showCompass: false }), "top-right");

      map.on("load", () => {
        if (!map) return;
        const line = points.filter((p) => p.kind !== "observation" && p.kind !== "position");
        if (connect && line.length >= 2) {
          map.addSource("link", {
            type: "geojson",
            data: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: line.map((p) => [p.lon, p.lat]) } },
          });
          map.addLayer({
            id: "link",
            type: "line",
            source: "link",
            paint: { "line-color": resolved === "dark" ? "#9cc6f2" : "#2b5c8f", "line-width": 2, "line-dasharray": [2, 2.5], "line-opacity": 0.85 },
          });
        }
      });

      for (const p of points) {
        const el = document.createElement("div");
        const size = p.kind === "observation" ? 10 : 16;
        el.className = "dl-marker";
        el.style.cssText = `width:${size}px;height:${size}px;border-radius:999px;background:${COLORS[p.kind]};border:2px solid ${resolved === "dark" ? "#151a21" : "#fff"};box-shadow:0 1px 3px rgba(0,0,0,.35);`;
        el.setAttribute("aria-label", p.label);
        el.setAttribute("role", "img");
        const popup = new ml.Popup({ offset: 10, closeButton: false }).setHTML(
          `<strong>${escapeHtml(p.label)}</strong>${p.detail ? `<br/>${escapeHtml(p.detail)}` : ""}`,
        );
        new ml.Marker({ element: el }).setLngLat([p.lon, p.lat]).setPopup(popup).addTo(map);
      }

      if (points.length > 1) {
        const b = new ml.LngLatBounds();
        for (const p of points) b.extend([p.lon, p.lat]);
        map.fitBounds(b, { padding: 48, maxZoom: 15, duration: 0 });
      }
    })();
    return () => {
      disposed = true;
      observer?.disconnect();
      map?.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, resolved]);

  return <div ref={ref} role="region" aria-label={ariaLabel} className={cn("h-full min-h-48 w-full overflow-hidden rounded-lg border border-border", className)} />;
}
