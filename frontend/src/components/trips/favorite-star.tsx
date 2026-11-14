"use client";

import { Star } from "lucide-react";
import { cn } from "@/lib/utils";

export function FavoriteStar({
  active,
  onToggle,
  className,
}: {
  active: boolean;
  onToggle: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      aria-pressed={active}
      aria-label={active ? "Ta bort favoritmarkering" : "Markera som favorit"}
      className={cn(
        "inline-flex size-7 items-center justify-center rounded-sm text-fg-subtle transition-colors hover:bg-surface-2 hover:text-undecided focus-visible:outline-2 focus-visible:outline-ring",
        active && "text-undecided",
        className,
      )}
    >
      <Star className="size-4" fill={active ? "currentColor" : "none"} aria-hidden />
    </button>
  );
}
