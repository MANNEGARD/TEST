import * as React from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Hopfällbar sektion (native <details>) för sekundär/teknisk information.
 * Tangentbordsvänlig utan JavaScript.
 */
export function CollapsibleSection({
  title,
  meta,
  icon,
  defaultOpen,
  children,
  className,
  id,
}: {
  title: React.ReactNode;
  meta?: React.ReactNode;
  icon?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <details id={id} open={defaultOpen} className={cn("group rounded-lg border border-border bg-surface shadow-sm", className)}>
      <summary className="flex cursor-pointer list-none items-center gap-3 rounded-lg px-5 py-3.5 select-none hover:bg-surface-2/50 focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 text-fg-subtle transition-transform group-open:rotate-90" aria-hidden />
        {icon ? <span className="text-fg-subtle [&_svg]:size-4">{icon}</span> : null}
        <span className="text-sm font-semibold">{title}</span>
        {meta ? <span className="ml-auto text-xs text-fg-muted">{meta}</span> : null}
      </summary>
      <div className="border-t border-border px-5 py-4">{children}</div>
    </details>
  );
}
