"use client";

import * as React from "react";
import { Eye, EyeOff } from "lucide-react";
import { Input } from "@/components/ui/input";

export const MIN_PASSWORD_LENGTH = 10;

export const PasswordInput = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>((props, ref) => {
  const [show, setShow] = React.useState(false);
  return (
    <div className="relative">
      <Input ref={ref} type={show ? "text" : "password"} className="pr-10" {...props} />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        className="absolute inset-y-0 right-0 flex w-10 items-center justify-center rounded-r-md text-fg-subtle hover:text-fg focus-visible:outline-2 focus-visible:outline-ring"
        aria-label={show ? "Dölj lösenord" : "Visa lösenord"}
        aria-pressed={show}
      >
        {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
      </button>
    </div>
  );
});
PasswordInput.displayName = "PasswordInput";

/** Lokala lösenordskrav (backend kontrollerar samma regler). */
export function passwordProblems(pw: string): { ok: boolean; label: string }[] {
  const common = ["password123", "lösenord123", "1234567890", "qwertyuiop"].includes(pw.toLowerCase());
  return [
    { ok: pw.length >= MIN_PASSWORD_LENGTH, label: `Minst ${MIN_PASSWORD_LENGTH} tecken` },
    { ok: pw.length > 0 && !common, label: "Inte ett vanligt lösenord" },
  ];
}
