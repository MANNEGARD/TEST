import { cn } from "@/lib/utils";

export function Kbd({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[4px] border border-border-strong bg-surface px-1 font-mono text-[11px] font-medium text-fg-muted shadow-[0_1px_0_var(--border-strong)]",
        className,
      )}
      {...props}
    />
  );
}
