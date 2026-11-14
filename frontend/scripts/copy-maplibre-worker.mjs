// Kopierar MapLibre GL:s web worker till public/vendor så att den serveras från
// samma origin (bundlern kan inte följa maplibres dynamiska worker-URL).
// Körs automatiskt efter npm install (postinstall).
import { copyFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const src = path.join(root, "node_modules", "maplibre-gl", "dist", "maplibre-gl-worker.mjs");
const dest = path.join(root, "public", "vendor", "maplibre-gl-worker.mjs");
try {
  await mkdir(path.dirname(dest), { recursive: true });
  await copyFile(src, dest);
  console.log("maplibre-gl-worker.mjs → public/vendor/");
} catch (err) {
  console.warn("Kunde inte kopiera MapLibre-workern:", err.message);
}
