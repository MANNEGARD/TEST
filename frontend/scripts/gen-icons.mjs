// Genererar PWA-ikoner (PNG) ur logotypens SVG med sharp.
// Kör: npm run gen:icons
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = path.join(root, "public", "icons");
const svg = await readFile(path.join(out, "icon.svg"));

// Maskable-varianten behöver säkerhetsmarginal (~20 %) och helfylld bakgrund.
const maskable = Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">
  <rect width="40" height="40" fill="#2b5c8f"/>
  <g transform="translate(4 4)">${svg.toString().replace(/<\/?svg[^>]*>/g, "").replace(/<rect[^>]*\/>/, "")}</g>
</svg>`);

await mkdir(out, { recursive: true });
const jobs = [
  ["icon-192.png", svg, 192],
  ["icon-512.png", svg, 512],
  ["apple-touch-icon.png", maskable, 180],
  ["icon-maskable-512.png", maskable, 512],
  ["favicon-32.png", svg, 32],
];
for (const [name, src, size] of jobs) {
  await sharp(src, { density: 600 }).resize(size, size).png().toFile(path.join(out, name));
  console.log(`skrev public/icons/${name}`);
}
