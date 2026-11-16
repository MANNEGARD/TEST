"use client";

import { useEffect } from "react";

/** Registrerar service workern (public/sw.js) i produktion. */
export function ServiceWorkerRegistration() {
  useEffect(() => {
    // Demot (statisk export) har ingen service worker.
    if (process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1") return;
    if (process.env.NODE_ENV !== "production" || !("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      /* PWA-stöd är frivilligt */
    });
  }, []);
  return null;
}
