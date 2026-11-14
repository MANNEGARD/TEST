import { MapPinOff } from "lucide-react";
import { cn } from "@/lib/utils";

/** Visas i stället för en karta när koordinater saknas eller kartor är avstängda. */
export function MapPlaceholder({ title, description, className }: { title: string; description?: string; className?: string }) {
  return (
    <div
      role="img"
      aria-label={title}
      className={cn(
        "relative flex h-full min-h-48 flex-col items-center justify-center gap-2 overflow-hidden rounded-lg border border-dashed border-border-strong bg-surface-2 px-6 text-center",
        className,
      )}
    >
      <svg aria-hidden className="absolute inset-0 h-full w-full text-border" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <pattern id="map-grid" width="28" height="28" patternUnits="userSpaceOnUse">
            <path d="M28 0H0V28" fill="none" stroke="currentColor" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#map-grid)" opacity="0.6" />
      </svg>
      <div className="relative flex size-10 items-center justify-center rounded-full border border-border bg-surface text-fg-subtle">
        <MapPinOff className="size-5" />
      </div>
      <p className="relative text-sm font-medium text-fg">{title}</p>
      {description ? <p className="relative max-w-sm text-xs leading-relaxed text-fg-muted">{description}</p> : null}
    </div>
  );
}
