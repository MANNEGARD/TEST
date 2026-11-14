"use client";

import * as React from "react";
import { BadgeCheck, Briefcase, Download, Home, Merge, PenLine, Star, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { plural } from "@/lib/format";

export interface BulkHandlers {
  onCategory: (c: "work" | "private") => void;
  onPurpose: (text: string) => void;
  onApprove: () => void;
  onFavorite: () => void;
  onMerge: () => void;
  onExport: () => void;
  onClear: () => void;
}

/** Massåtgärder för markerade resor. Visas ovanför tabellen när något är markerat. */
export function BulkBar({ count, busy, handlers }: { count: number; busy?: boolean; handlers: BulkHandlers }) {
  const [purpose, setPurpose] = React.useState("");
  const [open, setOpen] = React.useState(false);
  if (!count) return null;
  return (
    <div
      role="toolbar"
      aria-label="Åtgärder för markerade resor"
      className="sticky top-14 z-10 flex flex-wrap items-center gap-1.5 border-b border-border bg-accent-soft/70 px-3 py-2 backdrop-blur animate-fade-in"
    >
      <span className="mr-1 text-sm font-semibold text-accent-ink num">{plural(count, "vald", "valda")}</span>
      <Button size="sm" variant="secondary" onClick={() => handlers.onCategory("work")} disabled={busy}>
        <Briefcase className="text-work" /> Arbete
      </Button>
      <Button size="sm" variant="secondary" onClick={() => handlers.onCategory("private")} disabled={busy}>
        <Home className="text-private" /> Privat
      </Button>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button size="sm" variant="secondary" disabled={busy}>
            <PenLine /> Sätt syfte
          </Button>
        </PopoverTrigger>
        <PopoverContent>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (!purpose.trim()) return;
              handlers.onPurpose(purpose.trim());
              setPurpose("");
              setOpen(false);
            }}
            className="space-y-2"
          >
            <label htmlFor="bulk-purpose" className="text-[13px] font-medium">
              Syfte för {plural(count, "resa", "resor")}
            </label>
            <Input id="bulk-purpose" autoFocus value={purpose} onChange={(e) => setPurpose(e.target.value)} maxLength={500} />
            <Button type="submit" size="sm" variant="primary" className="w-full" disabled={!purpose.trim()}>
              Spara syfte
            </Button>
          </form>
        </PopoverContent>
      </Popover>
      <Button size="sm" variant="secondary" onClick={handlers.onApprove} disabled={busy}>
        <BadgeCheck /> Godkänn
      </Button>
      <Button size="sm" variant="secondary" onClick={handlers.onFavorite} disabled={busy}>
        <Star /> Favorit
      </Button>
      <Button size="sm" variant="secondary" onClick={handlers.onMerge} disabled={busy || count < 2} title={count < 2 ? "Markera minst två resor" : undefined}>
        <Merge /> Slå samman
      </Button>
      <Button size="sm" variant="secondary" onClick={handlers.onExport}>
        <Download /> Exportera valda
      </Button>
      <Button size="sm" variant="ghost" onClick={handlers.onClear} className="ml-auto">
        <X /> Avmarkera
      </Button>
    </div>
  );
}
