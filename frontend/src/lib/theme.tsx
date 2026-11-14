"use client";

/**
 * Tema: "system" (standard), "light" eller "dark". Sparas i localStorage under
 * `drivelog.theme`. Det upplösta temat sätts som data-theme på <html>; ett inline-
 * skript (THEME_INIT_SCRIPT, körs i <head>) gör samma sak före första målningen.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "drivelog.theme";

export const THEME_INIT_SCRIPT = `(function(){try{var p=localStorage.getItem('${THEME_STORAGE_KEY}');var d=p==='dark'||((!p||p==='system')&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.setAttribute('data-theme',d?'dark':'light');}catch(e){document.documentElement.setAttribute('data-theme','light');}})();`;

interface ThemeContextValue {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (p: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

// Externa källor (localStorage + systemets färgschema) läses med useSyncExternalStore
// så att serverrenderingen ("system"/ljust) och hydreringen alltid stämmer överens.
const listeners = new Set<() => void>();
function emit() {
  for (const l of listeners) l();
}

function subscribePreference(cb: () => void) {
  listeners.add(cb);
  const onStorage = (e: StorageEvent) => e.key === THEME_STORAGE_KEY && cb();
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    window.removeEventListener("storage", onStorage);
  };
}

function readPreference(): ThemePreference {
  try {
    const p = window.localStorage.getItem(THEME_STORAGE_KEY);
    return p === "light" || p === "dark" ? p : "system";
  } catch {
    return "system";
  }
}

function subscribeSystem(cb: () => void) {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

function readSystem(): ResolvedTheme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const preference = useSyncExternalStore(subscribePreference, readPreference, () => "system" as ThemePreference);
  const system = useSyncExternalStore(subscribeSystem, readSystem, () => "light" as ResolvedTheme);
  const resolved: ResolvedTheme = preference === "system" ? system : preference;

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", resolved);
    const meta = document.querySelector('meta[name="theme-color"]:not([media])');
    if (meta) meta.setAttribute("content", resolved === "dark" ? "#0e1116" : "#f7f7f5");
  }, [resolved]);

  const setPreference = useCallback((p: ThemePreference) => {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, p);
    } catch {
      /* ignorera */
    }
    emit();
  }, []);

  const value = useMemo(() => ({ preference, resolved, setPreference }), [preference, resolved, setPreference]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme måste användas inom <ThemeProvider>");
  return ctx;
}
