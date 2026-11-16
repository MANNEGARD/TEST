/**
 * Sista steget i `npm run build:demo` (scripts/build-demo.mjs): samlar den statiska exporten i out-demo/.
 *
 *  - .next-demo/ (output "export" med distDir .next-demo) → out-demo/
 *  - demo-static/startdata.json och korjournal-exempel.xlsx → out-demo/demo/
 *  - demo-static/_headers och robots.txt → out-demo/
 *  - ingen service worker i demot (sw.js tas bort)
 * Skriver katalogens storlek.
 */
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "out-demo");
const candidates = [path.join(ROOT, ".next-demo"), path.join(ROOT, "out")];
const exported = candidates.find((dir) => existsSync(path.join(dir, "index.html")));
if (!exported) {
  console.error("Hittar ingen statisk export (index.html) i .next-demo/ eller out/.");
  process.exit(1);
}

rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });
const skip = new Set(["cache", "server", "types", "diagnostics", "trace", "build", "dev"]);
for (const entry of readdirSync(exported)) {
  if (exported.endsWith(".next-demo") && (skip.has(entry) || entry.endsWith(".json") || entry === "BUILD_ID" || entry.startsWith("trace"))) continue;
  cpSync(path.join(exported, entry), path.join(OUT, entry), { recursive: true });
}
rmSync(path.join(OUT, "sw.js"), { force: true });

const STATIC = path.join(ROOT, "demo-static");
mkdirSync(path.join(OUT, "demo"), { recursive: true });
for (const f of ["startdata.json", "korjournal-exempel.xlsx"]) cpSync(path.join(STATIC, f), path.join(OUT, "demo", f));
for (const f of ["_headers", "robots.txt"]) cpSync(path.join(STATIC, f), path.join(OUT, f));

function size(dir) {
  let bytes = 0;
  let files = 0;
  for (const entry of readdirSync(dir)) {
    const p = path.join(dir, entry);
    const st = statSync(p);
    if (st.isDirectory()) {
      const sub = size(p);
      bytes += sub.bytes;
      files += sub.files;
    } else {
      bytes += st.size;
      files += 1;
    }
  }
  return { bytes, files };
}
const { bytes, files } = size(OUT);
console.log(`Demot är klart i ${path.relative(ROOT, OUT)}/: ${files} filer, ${(bytes / 1024 / 1024).toFixed(1)} MB.`);
