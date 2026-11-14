import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Slår ihop Tailwind-klasser och löser konflikter (shadcn-konvention). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Säker JSON-läsning från sessionStorage/localStorage (kan kasta i privat läge). */
export function readStorage<T>(storage: "local" | "session", key: string, fallback: T): T {
  try {
    const s = storage === "local" ? window.localStorage : window.sessionStorage;
    const raw = s.getItem(key);
    return raw == null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writeStorage(storage: "local" | "session", key: string, value: unknown): void {
  try {
    const s = storage === "local" ? window.localStorage : window.sessionStorage;
    if (value === undefined) s.removeItem(key);
    else s.setItem(key, JSON.stringify(value));
  } catch {
    /* lagring kan vara blockerad – ignorera */
  }
}

/** Plattformsberoende etikett för kortkommandon (⌘ på Mac, Ctrl annars). */
export function modKeyLabel(): string {
  if (typeof navigator === "undefined") return "Ctrl";
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl";
}

/** Sant om händelsen kommer från ett textfält (för globala tangentbordsgenvägar). */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}
