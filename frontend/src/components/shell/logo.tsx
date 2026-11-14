import { cn } from "@/lib/utils";

/** DriveLogs logotyp: mätartavla i en avrundad ruta. Samma motiv som app-ikonerna. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={cn("size-7", className)} aria-hidden focusable="false">
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <path d="M8.2 21.5a9 9 0 1 1 15.6 0" fill="none" stroke="var(--accent-fg)" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M16 19.2 20.6 11.6" stroke="var(--accent-fg)" strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="16" cy="19.4" r="2.2" fill="var(--accent-fg)" />
      <path d="M11 25h10" stroke="#5fd3c6" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <LogoMark />
      <span className="flex flex-col leading-none">
        <span className="text-[10px] font-semibold tracking-[0.14em] text-fg-subtle">SMGD</span>
        <span className="text-[15px] font-semibold tracking-tight text-fg">DriveLog</span>
      </span>
    </span>
  );
}
