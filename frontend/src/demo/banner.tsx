"use client";

import { FlaskConical, RotateCcw } from "lucide-react";

/** Nycklar i localStorage som demot nollställer (temat behålls). */
function clearDemo(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith("drivelog.") && k !== "drivelog.theme") keys.push(k);
    }
    for (const k of keys) window.localStorage.removeItem(k);
    window.sessionStorage.clear();
  } catch {
    /* blockerad lagring: inget sparat att rensa */
  }
}

/** Alltid synlig märkning i demot, med "Återställ demot". */
export default function DemoBanner() {
  const reset = () => {
    if (!window.confirm("Återställ demot? Allt du har ändrat försvinner och demot börjar om.")) return;
    clearDemo();
    // Full omladdning (inte router.push) så att demots tillstånd i minnet släpps.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/");
  };
  return (
    <div
      role="note"
      data-testid="demo-banner"
      className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 border-b border-accent/30 bg-accent-soft px-4 py-1.5 text-center text-[12.5px] text-accent-ink"
    >
      <span className="inline-flex items-center gap-1.5 font-semibold">
        <FlaskConical className="size-3.5" aria-hidden /> Demo
      </span>
      <span>Påhittade uppgifter, inget sparas på någon server.</span>
      <button
        type="button"
        onClick={reset}
        className="inline-flex items-center gap-1 rounded-sm font-semibold underline underline-offset-2 hover:no-underline focus-visible:outline-2 focus-visible:outline-ring"
      >
        <RotateCcw className="size-3.5" aria-hidden /> Återställ demot
      </button>
    </div>
  );
}
