import * as React from "react";
import { cn } from "@/lib/utils";

/** Beskrivningslista: etikett/värde-par i rutnät. */
export function DescriptionList({ className, ...props }: React.HTMLAttributes<HTMLDListElement>) {
  return <dl className={cn("grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3", className)} {...props} />;
}

export function DescriptionItem({ label, children, className }: { label: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="mt-1 text-sm text-fg num">{children}</dd>
    </div>
  );
}
