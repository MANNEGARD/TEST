"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { Button, type ButtonProps } from "./button";

/**
 * Knapp för sådant som inte går att ångra: första trycket visar vad som händer,
 * andra genomför. Ingen modal – bekräftelsen visas på samma plats.
 */
export function ConfirmInline({
  label,
  confirmLabel,
  description,
  onConfirm,
  loading,
  icon,
  size = "sm",
  ariaLabel,
  className,
  triggerClassName,
}: {
  label: string;
  confirmLabel: string;
  description: React.ReactNode;
  onConfirm: () => void;
  loading?: boolean;
  icon?: React.ReactNode;
  size?: ButtonProps["size"];
  /** Tydligare etikett för skärmläsare, t.ex. "Återkalla token Home Assistant". */
  ariaLabel?: string;
  /** Klasser för bekräftelserutan. */
  className?: string;
  /** Klasser för den första knappen. */
  triggerClassName?: string;
}) {
  const [armed, setArmed] = React.useState(false);
  if (!armed) {
    return (
      <Button variant="danger-ghost" size={size} onClick={() => setArmed(true)} aria-label={ariaLabel} className={triggerClassName}>
        {icon}
        {label}
      </Button>
    );
  }
  return (
    <div role="group" aria-label={ariaLabel ?? label} className={cn("rounded-lg border border-err/30 bg-err-soft/60 p-3", className)}>
      <p className="text-[13px] leading-relaxed text-fg">{description}</p>
      <div className="mt-2.5 flex flex-wrap gap-2">
        <Button variant="danger" size="sm" loading={loading} onClick={onConfirm} autoFocus>
          {confirmLabel}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setArmed(false)} disabled={loading}>
          Avbryt
        </Button>
      </div>
    </div>
  );
}
