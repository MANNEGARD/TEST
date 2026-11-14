import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const badgeVariants = cva(
  "inline-flex max-w-full items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium leading-4 [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      tone: {
        neutral: "border-border bg-surface-2 text-fg-muted",
        accent: "border-transparent bg-accent-soft text-accent-ink",
        work: "border-transparent bg-work-soft text-work-ink",
        private: "border-transparent bg-private-soft text-private-ink",
        undecided: "border-transparent bg-undecided-soft text-undecided-ink",
        ok: "border-transparent bg-ok-soft text-ok-ink",
        warn: "border-transparent bg-warn-soft text-warn-ink",
        err: "border-transparent bg-err-soft text-err-ink",
        fuel: "border-transparent bg-fuel-soft text-fuel-ink",
        el: "border-transparent bg-el-soft text-el-ink",
        outline: "border-border bg-transparent text-fg-muted",
      },
      size: {
        sm: "px-1.5 py-0 text-[11px]",
        md: "",
      },
    },
    defaultVariants: { tone: "neutral", size: "md" },
  },
);

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(({ className, tone, size, ...props }, ref) => (
  <span ref={ref} className={cn(badgeVariants({ tone, size }), className)} {...props} />
));
Badge.displayName = "Badge";
