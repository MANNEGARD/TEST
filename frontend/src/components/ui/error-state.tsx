"use client";

import { AlertTriangle, RotateCw } from "lucide-react";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "./button";

/** Felruta med begriplig text (backendens `detail`) och "Försök igen". */
export function ErrorState({
  error,
  onRetry,
  title = "Det gick inte att hämta uppgifterna",
  className,
  compact,
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center gap-2 text-center",
        compact ? "px-4 py-6" : "px-6 py-12",
        className,
      )}
    >
      <div className="flex size-10 items-center justify-center rounded-full bg-err-soft text-err-ink">
        <AlertTriangle className="size-5" aria-hidden />
      </div>
      <h3 className="text-sm font-semibold text-fg">{title}</h3>
      <p className="max-w-md text-sm text-fg-muted">{errorMessage(error)}</p>
      {onRetry ? (
        <Button size="sm" variant="secondary" onClick={onRetry} className="mt-1">
          <RotateCw aria-hidden /> Försök igen
        </Button>
      ) : null}
    </div>
  );
}
