"use client";

import * as React from "react";
import { Check, Pencil, X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Syfte som redigeras direkt på plats. Klick/Enter öppnar fältet, Enter sparar,
 * Escape avbryter. Tomt syfte visas som dämpad platshållare.
 */
export function InlinePurpose({
  value,
  onSave,
  placeholder = "Lägg till syfte",
  suggestions = [],
  className,
  confirmed = true,
}: {
  value: string | null | undefined;
  onSave: (text: string) => void;
  placeholder?: string;
  suggestions?: string[];
  className?: string;
  /** Falskt = föreslaget syfte som inte bekräftats (visas kursivt med "Förslag"). */
  confirmed?: boolean;
}) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(value ?? "");
  const inputRef = React.useRef<HTMLInputElement>(null);
  const listId = React.useId();

  React.useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commit = () => {
    const text = draft.trim();
    setEditing(false);
    if (text !== (value ?? "").trim() && text) onSave(text);
  };

  if (editing) {
    return (
      <div className={cn("flex min-w-48 items-center gap-1", className)} onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={draft}
          list={suggestions.length ? listId : undefined}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              setDraft(value ?? "");
              setEditing(false);
            }
          }}
          maxLength={500}
          aria-label="Syfte"
          className="h-7 w-full min-w-0 rounded-sm border border-ring bg-surface px-2 text-[13px] text-fg outline-none"
        />
        {suggestions.length ? (
          <datalist id={listId}>
            {suggestions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        ) : null}
        <button type="button" onClick={commit} aria-label="Spara syfte" className="rounded-sm p-1 text-ok-ink hover:bg-ok-soft">
          <Check className="size-3.5" />
        </button>
        <button
          type="button"
          onClick={() => {
            setDraft(value ?? "");
            setEditing(false);
          }}
          aria-label="Avbryt"
          className="rounded-sm p-1 text-fg-subtle hover:bg-surface-2"
        >
          <X className="size-3.5" />
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        setDraft(value ?? "");
        setEditing(true);
      }}
      className={cn(
        "group/purpose -mx-1 inline-flex max-w-full items-center gap-1.5 rounded-sm px-1 py-0.5 text-left text-[13px] hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-ring",
        className,
      )}
      aria-label={value ? `Syfte: ${value}. Klicka för att ändra` : "Lägg till syfte"}
    >
      <span className={cn("truncate", !value && "text-fg-subtle", value && !confirmed && "text-fg-muted italic")}>
        {value || placeholder}
      </span>
      <Pencil className="size-3 shrink-0 text-fg-subtle opacity-0 transition-opacity group-hover/purpose:opacity-100 group-focus-visible/purpose:opacity-100" aria-hidden />
    </button>
  );
}
