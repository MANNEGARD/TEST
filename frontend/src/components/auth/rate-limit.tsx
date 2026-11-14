"use client";

import * as React from "react";

/** Nedräkning för 429-svar. Returnerar kvarvarande sekunder (0 = får försöka igen). */
export function useCountdown(seconds: number | null): number {
  const [left, setLeft] = React.useState(seconds ?? 0);
  React.useEffect(() => {
    setLeft(seconds ?? 0);
    if (!seconds) return;
    const started = Date.now();
    const id = window.setInterval(() => {
      const remaining = Math.max(0, seconds - Math.floor((Date.now() - started) / 1000));
      setLeft(remaining);
      if (remaining === 0) window.clearInterval(id);
    }, 500);
    return () => window.clearInterval(id);
  }, [seconds]);
  return left;
}
