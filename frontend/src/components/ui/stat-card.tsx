import * as React from "react";
import { cn } from "@/lib/utils";
import { Skeleton } from "./skeleton";

/**
 * Nyckeltalsruta. Värdet visas med tabulära siffror i textens typsnitt. Använd
 * <QualityValue> som `value` när värdet kan vara uppskattat eller saknas.
 * `accent` ger en tunn färgmarkering (kategori/energi) – alltid tillsammans med text.
 */
export function StatCard({
  label,
  value,
  unit,
  sub,
  footer,
  icon,
  accent,
  loading,
  className,
  href,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  unit?: React.ReactNode;
  sub?: React.ReactNode;
  footer?: React.ReactNode;
  icon?: React.ReactNode;
  accent?: "work" | "private" | "undecided" | "fuel" | "el" | "accent" | "warn" | "ok";
  loading?: boolean;
  className?: string;
  href?: string;
}) {
  const accentBar = accent
    ? {
        work: "bg-work",
        private: "bg-private",
        undecided: "bg-undecided",
        fuel: "bg-fuel",
        el: "bg-el",
        accent: "bg-accent",
        warn: "bg-warn",
        ok: "bg-ok",
      }[accent]
    : null;
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 text-[13px] font-medium text-fg-muted">
          {accentBar ? <span className={cn("h-3 w-1 shrink-0 rounded-full", accentBar)} aria-hidden /> : null}
          <span className="truncate">{label}</span>
        </div>
        {icon ? <span className="text-fg-subtle [&_svg]:size-4">{icon}</span> : null}
      </div>
      {loading ? (
        <Skeleton className="mt-3 h-7 w-24" />
      ) : (
        <div className="mt-2 flex items-baseline gap-1.5">
          <span className="text-[21px] leading-none font-medium tracking-tight text-fg num sm:text-[26px]">{value}</span>
          {unit ? <span className="text-sm text-fg-muted">{unit}</span> : null}
        </div>
      )}
      {sub && !loading ? <div className="mt-1.5 text-xs leading-relaxed text-fg-muted">{sub}</div> : null}
      {footer && !loading ? <div className="mt-3 border-t border-border pt-2.5 text-xs text-fg-muted">{footer}</div> : null}
    </>
  );
  const cls = cn(
    "flex min-w-0 flex-col rounded-lg border border-border bg-surface p-3.5 shadow-sm sm:p-4",
    href && "transition-colors hover:border-border-strong focus-visible:outline-2 focus-visible:outline-ring",
    className,
  );
  if (href) {
    return (
      <a href={href} className={cls}>
        {body}
      </a>
    );
  }
  return <div className={cls}>{body}</div>;
}
