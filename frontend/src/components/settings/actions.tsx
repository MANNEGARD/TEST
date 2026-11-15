"use client";

/**
 * Små åtgärdskomponenter för inställningssidan: kopiera till urklipp och en
 * bekräftelse på plats för sådant som inte går att ångra (ingen modal).
 */

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { toast } from "sonner";
import { Button, type ButtonProps } from "@/components/ui/button";

async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = React.useState(false);
  React.useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <Button
      size="icon"
      variant="secondary"
      aria-label={label}
      title={label}
      onClick={async () => {
        if (await copyText(value)) {
          setCopied(true);
          toast.success("Kopierat till urklipp.");
        } else {
          toast.error("Det gick inte att kopiera – markera texten och kopiera manuellt.");
        }
      }}
    >
      {copied ? <Check className="text-ok" aria-hidden /> : <Copy aria-hidden />}
    </Button>
  );
}

/**
 * Knapp för sådant som inte går att ångra: första trycket visar vad som händer,
 * andra genomför. Bekräftelsen visas på samma plats.
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
}) {
  const [armed, setArmed] = React.useState(false);
  if (!armed) {
    return (
      <Button variant="danger-ghost" size={size} onClick={() => setArmed(true)} aria-label={ariaLabel}>
        {icon}
        {label}
      </Button>
    );
  }
  return (
    <div role="group" aria-label={ariaLabel ?? label} className="w-full rounded-lg border border-err/30 bg-err-soft/60 p-3">
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
