import * as React from "react";
import { cn } from "@/lib/utils";

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  compact,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "gap-2 px-4 py-8" : "gap-3 px-6 py-14", className)}>
      {icon ? (
        <div className="mb-1 flex size-11 items-center justify-center rounded-full border border-border bg-surface-2 text-fg-subtle [&_svg]:size-5">
          {icon}
        </div>
      ) : null}
      <h3 className="text-[15px] font-semibold text-fg">{title}</h3>
      {description ? <p className="max-w-md text-sm leading-relaxed text-fg-muted">{description}</p> : null}
      {action ? <div className="mt-2 flex flex-wrap items-center justify-center gap-2">{action}</div> : null}
    </div>
  );
}
