"use client";

import * as React from "react";
import "maplibre-gl/dist/maplibre-gl.css";
import { MousePointerClick } from "lucide-react";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export interface LatLon {
  lat: number;
  lon: number;
}

/** Svenska texter för MapLibres egna kontroller och hjälptexter. */
const MAP_LOCALE: Record<string, string> = {
  "AttributionControl.ToggleAttribution": "Visa eller dölj källhänvisning",
  "NavigationControl.ZoomIn": "Zooma in",
  "NavigationControl.ZoomOut": "Zooma ut",
  "CooperativeGesturesHandler.WindowsHelpText": "Håll ned Ctrl och skrolla för att zooma kartan",
  "CooperativeGesturesHandler.MacHelpText": "Håll ned ⌘ och skrolla för att zooma kartan",
  "CooperativeGesturesHandler.MobileHelpText": "Använd två fingrar för att flytta kartan",
};

/** Ungefärlig ram runt Sverige – startläge när ingen plats har position ännu. */
const SWEDEN: [[number, number], [number, number]] = [
  [10.6, 55.2],
  [24.2, 69.1],
];

function round6(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}

/** Cirkel (polygon) med radie i meter kring en punkt – ritar geofencen. */
function circle(center: LatLon, radiusM: number, steps = 72) {
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.max(0.01, Math.cos((center.lat * Math.PI) / 180)));
  const ring: [number, number][] = [];
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * 2 * Math.PI;
    ring.push([center.lon + dLon * Math.sin(a), center.lat + dLat * Math.cos(a)]);
  }
  return { type: "Feature" as const, properties: {}, geometry: { type: "Polygon" as const, coordinates: [ring] } };
}

const EMPTY = { type: "FeatureCollection" as const, features: [] };

function attributionFor(url: string): string {
  if (url.includes("openstreetmap")) return '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
  return "";
}

interface MapState {
  map: import("maplibre-gl").Map;
  ml: typeof import("maplibre-gl");
  marker: import("maplibre-gl").Marker | null;
  loaded: boolean;
  /** Senast ritade position ("lat,lon") – för att upptäcka ändringar utifrån. */
  drawn: string | null;
  /** Sant när ändringen kom från klick/drag i kartan (då flyttas inte utsnittet). */
  fromMap: boolean;
}

/** Zoomar så att hela geofencen syns med lite luft omkring. */
function fitFence(s: MapState, center: LatLon, radiusM: number, animate: boolean) {
  const ring = circle(center, Math.max(radiusM, 60) * 1.6).geometry.coordinates[0];
  const b = new s.ml.LngLatBounds();
  for (const c of ring) b.extend(c);
  s.map.fitBounds(b, { padding: 16, maxZoom: 17, duration: animate ? 350 : 0 });
}

/**
 * Karta där användaren sätter ut en plats: klick flyttar markören, markören kan dras
 * och geofencen ritas som en cirkel med vald radie. Kartan är ett komplement –
 * koordinaterna kan alltid anges i textfält bredvid (tangentbord/skärmläsare).
 */
export function PositionPicker({
  value,
  radius,
  onChange,
  tilesUrl,
  context = [],
  className,
}: {
  value: LatLon | null;
  /** Geofence-radie i meter. */
  radius: number;
  onChange: (value: LatLon) => void;
  tilesUrl: string;
  /** Andra platsers positioner – används för startutsnittet när `value` saknas. */
  context?: LatLon[];
  className?: string;
}) {
  const ref = React.useRef<HTMLDivElement>(null);
  const { resolved } = useTheme();
  const state = React.useRef<MapState | null>(null);
  const latest = React.useRef({ value, radius, onChange, context });

  const draw = React.useCallback(() => {
    const s = state.current;
    if (!s) return;
    const { value: v, radius: r } = latest.current;
    if (v) {
      if (!s.marker) {
        const el = document.createElement("div");
        el.className = "dl-marker";
        el.style.cssText =
          "width:18px;height:18px;border-radius:999px;background:var(--accent);border:2.5px solid var(--surface);box-shadow:0 1px 4px rgba(0,0,0,.45);cursor:grab;";
        el.setAttribute("role", "img");
        el.setAttribute("aria-label", "Platsens position – dra för att flytta");
        const marker = new s.ml.Marker({ element: el, draggable: true }).setLngLat([v.lon, v.lat]).addTo(s.map);
        marker.on("dragend", () => {
          const p = marker.getLngLat();
          s.fromMap = true;
          latest.current.onChange({ lat: round6(p.lat), lon: round6(p.lng) });
        });
        s.marker = marker;
      } else {
        s.marker.setLngLat([v.lon, v.lat]);
      }
    } else if (s.marker) {
      s.marker.remove();
      s.marker = null;
    }
    const key = v ? `${v.lat},${v.lon}` : null;
    if (key !== s.drawn) {
      // Koordinater som skrivits in för hand: flytta utsnittet dit och visa geofencen.
      if (v && !s.fromMap) fitFence(s, v, r, !window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      s.drawn = key;
    }
    s.fromMap = false;
    if (s.loaded) {
      const source = s.map.getSource("fence") as import("maplibre-gl").GeoJSONSource | undefined;
      source?.setData(v ? circle(v, r) : EMPTY);
    }
  }, []);

  React.useEffect(() => {
    latest.current = { value, radius, onChange, context };
    draw();
  });

  React.useEffect(() => {
    let disposed = false;
    let observer: ResizeObserver | null = null;
    (async () => {
      const ml = await import("maplibre-gl");
      if (disposed || !ref.current) return;
      ml.setWorkerUrl("/vendor/maplibre-gl-worker.mjs");
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const start = latest.current;
      const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2b5c8f";
      const map = new ml.Map({
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
        center: start.value ? [start.value.lon, start.value.lat] : [16, 62.5],
        zoom: start.value ? 15 : 3,
        attributionControl: { compact: true },
        cooperativeGestures: true,
        locale: MAP_LOCALE,
        fadeDuration: reduced ? 0 : 300,
      });
      const s: MapState = { map, ml, marker: null, loaded: false, drawn: start.value ? `${start.value.lat},${start.value.lon}` : null, fromMap: false };
      state.current = s;
      map.getCanvas().style.cursor = "crosshair";
      observer = new ResizeObserver(() => map.resize());
      observer.observe(ref.current);
      map.addControl(new ml.NavigationControl({ showCompass: false }), "top-right");

      if (start.value) {
        fitFence(s, start.value, start.radius, false);
      } else if (start.context.length) {
        const b = new ml.LngLatBounds();
        for (const c of start.context) b.extend([c.lon, c.lat]);
        map.fitBounds(b, { padding: 48, maxZoom: 11, duration: 0 });
      } else {
        map.fitBounds(SWEDEN, { padding: 8, duration: 0 });
      }

      map.on("click", (e) => {
        s.fromMap = true;
        latest.current.onChange({ lat: round6(e.lngLat.lat), lon: round6(e.lngLat.lng) });
      });
      map.on("load", () => {
        if (state.current !== s) return;
        map.addSource("fence", { type: "geojson", data: EMPTY });
        map.addLayer({ id: "fence-fill", type: "fill", source: "fence", paint: { "fill-color": accent, "fill-opacity": 0.14 } });
        map.addLayer({ id: "fence-line", type: "line", source: "fence", paint: { "line-color": accent, "line-width": 1.5, "line-opacity": 0.9 } });
        s.loaded = true;
        draw();
      });
      draw();
    })();
    return () => {
      disposed = true;
      observer?.disconnect();
      state.current?.map.remove();
      state.current = null;
    };
  }, [tilesUrl, resolved, draw]);

  return (
    <div className={cn("relative h-56 w-full overflow-hidden rounded-lg border border-border", className)}>
      <div ref={ref} role="region" aria-label="Karta – klicka för att sätta ut platsens position" className="h-full w-full" />
      {!value ? (
        <p className="pointer-events-none absolute top-2 left-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface/95 px-2 py-1 text-xs font-medium text-fg shadow-sm">
          <MousePointerClick className="size-3.5 text-accent" aria-hidden />
          Klicka på kartan för att sätta ut platsen
        </p>
      ) : null}
    </div>
  );
}
