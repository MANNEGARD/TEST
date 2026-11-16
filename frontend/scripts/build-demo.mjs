/**
 * `npm run build:demo`: bygger demot (NEXT_PUBLIC_DRIVELOG_DEMO=1, statisk export) utan att röra
 * det vanliga bygget.
 *
 * Med output "export" bygger Next.js alltid i .next/ och skriver exporten till distDir
 * (.next-demo/). Ett befintligt vanligt bygge i .next/ flyttas därför undan under tiden och
 * läggs tillbaka efteråt (även om bygget misslyckas). Sist samlar finish-demo.mjs exporten i
 * out-demo/.
 */
import { spawnSync } from "node:child_process";
import { existsSync, renameSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const NEXT = path.join(ROOT, ".next");
const KEEP = path.join(ROOT, ".next-vanligt-bygge");

if (existsSync(KEEP)) {
  // Ett tidigare avbrutet demobygge: det vanliga bygget ligger kvar här.
  rmSync(NEXT, { recursive: true, force: true });
  renameSync(KEEP, NEXT);
}
const hadNormal = existsSync(NEXT);
if (hadNormal) renameSync(NEXT, KEEP);

let status = 1;
try {
  const bin = path.join(ROOT, "node_modules", ".bin", process.platform === "win32" ? "next.cmd" : "next");
  const build = spawnSync(bin, ["build"], { cwd: ROOT, stdio: "inherit", env: { ...process.env, NEXT_PUBLIC_DRIVELOG_DEMO: "1" } });
  status = build.status ?? 1;
  if (status === 0) {
    const finish = spawnSync(process.execPath, [path.join(ROOT, "scripts", "finish-demo.mjs")], { cwd: ROOT, stdio: "inherit" });
    status = finish.status ?? 1;
  }
} finally {
  rmSync(NEXT, { recursive: true, force: true });
  if (hadNormal) renameSync(KEEP, NEXT);
}
process.exit(status);
