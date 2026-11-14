import * as React from "react";
import { cn } from "@/lib/utils";

/** Sidrubrik: h1 + beskrivning + åtgärder till höger. Används överst på varje sida. */
export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  className,
  children,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  eyebrow?: React.ReactNode;
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <header className={cn("mb-6 flex flex-col gap-4", className)}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          {eyebrow ? <div className="mb-1 text-xs font-medium text-fg-subtle">{eyebrow}</div> : null}
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight text-fg sm:text-2xl">{title}</h1>
          {description ? <p className="mt-1 max-w-2xl text-sm text-fg-muted">{description}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </header>
  );
}
