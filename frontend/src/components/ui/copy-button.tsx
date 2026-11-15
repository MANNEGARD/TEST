"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { toast } from "sonner";
import { Button, type ButtonProps } from "./button";

/** Kopierar text till urklipp – med reservväg för osäkra ursprung (http på lokalt nät). */
export async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* faller tillbaka nedan */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
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

/** Ikonknapp som kopierar `value` och visar en bock en kort stund. */
export function CopyButton({
  value,
  label,
  size = "icon",
  variant = "secondary",
}: { value: string; label: string } & Pick<ButtonProps, "size" | "variant">) {
  const [copied, setCopied] = React.useState(false);
  React.useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <Button
      size={size}
      variant={variant}
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
