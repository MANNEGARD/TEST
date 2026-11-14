"use client";

import * as React from "react";

/**
 * Nedräkning för 429-svar. `until` = tidsstämpel (ms) då man får försöka igen.
 * Returnerar kvarvarande hela sekunder (0 = får försöka igen).
 */
export function useCountdown(until: number | null): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!until) return;
    const id = window.setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= until) window.clearInterval(id);
    }, 500);
    return () => window.clearInterval(id);
  }, [until]);
  if (!until) return 0;
  return Math.max(0, Math.ceil((until - now) / 1000));
}

/** Tidsstämpel för när ett 429-svar med `seconds` väntetid löper ut. */
export function retryUntil(seconds: number | null | undefined): number {
  return Date.now() + (seconds ?? 60) * 1000;
}
