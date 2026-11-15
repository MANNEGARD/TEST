"use client";

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/** Sidopanel (höger) eller bottenark (mobil) byggd på Radix Dialog. */
export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

/**
 * Minns vilket element som hade fokus när panelen öppnades. Layout-effekten körs innan
 * Radix flyttar in fokus i panelen, så elementet är fortfarande det som öppnade den.
 */
function RememberOpener({ openerRef }: { openerRef: React.RefObject<HTMLElement | null> }) {
  React.useLayoutEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }, [openerRef]);
  return null;
}

export const SheetContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & { side?: "right" | "bottom" | "left" }
>(({ className, children, side = "right", onCloseAutoFocus, ...props }, ref) => {
  const openerRef = React.useRef<HTMLElement | null>(null);
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in" />
      <DialogPrimitive.Content
        ref={ref}
        className={cn(
          "fixed z-50 flex flex-col gap-4 overflow-y-auto border-border bg-surface p-5 shadow-lg focus-visible:outline-none",
          side === "right" && "inset-y-0 right-0 h-full w-[min(420px,100vw)] border-l animate-slide-in-right",
          side === "left" && "inset-y-0 left-0 h-full w-[min(300px,85vw)] border-r",
          side === "bottom" && "inset-x-0 bottom-0 max-h-[85dvh] rounded-t-xl border-t pb-[max(1.25rem,env(safe-area-inset-bottom))] animate-slide-in-bottom",
          className,
        )}
        onCloseAutoFocus={(event) => {
          onCloseAutoFocus?.(event);
          if (event.defaultPrevented) return;
          // Paneler som öppnas via state (utan SheetTrigger) lämnar annars fokus på <body>.
          const el = openerRef.current;
          openerRef.current = null;
          if (el?.isConnected) {
            event.preventDefault();
            el.focus();
          }
        }}
        {...props}
      >
        <RememberOpener openerRef={openerRef} />
        {children}
        <DialogPrimitive.Close
          className={cn(
            "z-10 rounded-sm bg-surface p-1 text-fg-subtle hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-ring",
            // Högerpanelen fyller hela höjden: fäst knappen mot fönstret så att den inte
            // rullar bort i långa paneler. Övriga lägen är korta och följer innehållet.
            side === "right" ? "fixed top-4 right-4" : "absolute top-4 right-4",
          )}
          aria-label="Stäng"
        >
          <X className="size-4" />
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});
SheetContent.displayName = "SheetContent";

export function SheetHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1 pr-8", className)} {...props} />;
}

export const SheetTitle = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn("text-base font-semibold", className)} {...props} />
));
SheetTitle.displayName = "SheetTitle";

export const SheetDescription = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn("text-sm text-fg-muted", className)} {...props} />
));
SheetDescription.displayName = "SheetDescription";
