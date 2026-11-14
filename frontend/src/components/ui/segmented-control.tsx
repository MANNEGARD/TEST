"use client";

import * as React from "react";
import * as ToggleGroup from "@radix-ui/react-toggle-group";
import { cn } from "@/lib/utils";

export interface SegmentOption<T extends string> {
  value: T;
  label: React.ReactNode;
  /** Klasser för valt läge, t.ex. kategorifärg. */
  activeClassName?: string;
  icon?: React.ReactNode;
  ariaLabel?: string;
  title?: string;
}

/**
 * Segmenterad kontroll (ett val). Byter värde med ETT klick; piltangenter flyttar
 * fokus (Radix ToggleGroup). Avmarkering är avstängd – ett värde är alltid valt
 * om `value` är satt.
 */
export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  size = "md",
  className,
  ariaLabel,
  disabled,
}: {
  value: T | undefined;
  onChange: (value: T) => void;
  options: SegmentOption<T>[];
  size?: "sm" | "md" | "lg";
  className?: string;
  ariaLabel: string;
  disabled?: boolean;
}) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value ?? ""}
      onValueChange={(v) => v && onChange(v as T)}
      aria-label={ariaLabel}
      disabled={disabled}
      className={cn("inline-flex items-center gap-0.5 rounded-md border border-border bg-surface-2 p-0.5", className)}
    >
      {options.map((o) => (
        <ToggleGroup.Item
          key={o.value}
          value={o.value}
          aria-label={o.ariaLabel}
          title={o.title}
          className={cn(
            "inline-flex items-center justify-center gap-1.5 rounded-[5px] font-medium whitespace-nowrap text-fg-muted transition-[background-color,color,box-shadow] duration-150 hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring disabled:opacity-50 data-[state=on]:bg-surface data-[state=on]:text-fg data-[state=on]:shadow-sm [&_svg]:size-3.5",
            size === "sm" && "h-6 px-2 text-xs",
            size === "md" && "h-7 px-2.5 text-[13px]",
            size === "lg" && "h-10 px-4 text-sm [&_svg]:size-4",
            value === o.value && o.activeClassName,
          )}
        >
          {o.icon}
          {o.label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
