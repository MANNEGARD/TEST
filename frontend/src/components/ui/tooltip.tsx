"use client";

import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cn } from "@/lib/utils";

export const TooltipProvider = TooltipPrimitive.Provider;
export const TooltipRoot = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export const TooltipContent = React.forwardRef<
  React.ComponentRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 6, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        "z-[60] max-w-72 rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs leading-relaxed text-fg shadow-md animate-fade-in",
        className,
      )}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = "TooltipContent";

/**
 * Enkel tooltip: <Tooltip content="Förklaring"><span tabIndex={0}>…</span></Tooltip>
 * Barnet måste kunna ta fokus (knapp, länk eller tabIndex={0}) för tangentbordsåtkomst.
 */
export function Tooltip({
  content,
  children,
  side,
  className,
  delayDuration,
}: {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  className?: string;
  delayDuration?: number;
}) {
  if (content == null || content === "") return <>{children}</>;
  return (
    <TooltipPrimitive.Root delayDuration={delayDuration}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipContent side={side} className={className}>
        {content}
      </TooltipContent>
    </TooltipPrimitive.Root>
  );
}
