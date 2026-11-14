"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { CornerDownLeft, Search } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { cn, modKeyLabel } from "@/lib/utils";
import { NAV_ITEMS } from "./nav-items";

/**
 * Global sökning. ⌘K / Ctrl+K öppnar. Enter söker i Körjournalen (/journal?q=…);
 * matchande sidor visas som genvägar.
 */
export function CommandSearch() {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");
  const [active, setActive] = React.useState(0);
  const [mod, setMod] = React.useState("Ctrl");

  React.useEffect(() => {
    setMod(modKeyLabel());
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const term = q.trim();
  const pages = NAV_ITEMS.filter((i) => !term || i.label.toLowerCase().includes(term.toLowerCase()));
  const options = [
    ...(term ? [{ key: "search", label: `Sök "${term}" i körjournalen`, href: `/journal?q=${encodeURIComponent(term)}` }] : []),
    ...pages.map((p) => ({ key: p.href, label: p.label, href: p.href, icon: p.icon })),
  ];

  const go = (href: string) => {
    setOpen(false);
    setQ("");
    router.push(href);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="hidden h-9 w-full max-w-xs items-center gap-2 rounded-md border border-border bg-surface-2/70 px-3 text-sm text-fg-subtle transition-colors hover:border-border-strong hover:text-fg-muted focus-visible:outline-2 focus-visible:outline-ring md:flex"
        aria-label="Sök resor"
        aria-keyshortcuts="Control+K Meta+K"
      >
        <Search className="size-4" aria-hidden />
        <span className="flex-1 text-left">Sök resor, adresser, syften…</span>
        <Kbd>{mod}</Kbd>
        <Kbd>K</Kbd>
      </button>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex size-9 items-center justify-center rounded-md text-fg-muted hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-ring md:hidden"
        aria-label="Sök"
      >
        <Search className="size-5" aria-hidden />
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent hideClose className="top-[14vh] max-w-xl translate-y-0 gap-0 overflow-hidden p-0">
          <DialogTitle className="sr-only">Sök</DialogTitle>
          <DialogDescription className="sr-only">Sök i körjournalen eller hoppa till en sida.</DialogDescription>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const opt = options[active];
              if (opt) go(opt.href);
            }}
            className="flex items-center gap-2 border-b border-border px-4"
          >
            <Search className="size-4 text-fg-subtle" aria-hidden />
            <input
              autoFocus
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setActive((a) => Math.min(options.length - 1, a + 1));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setActive((a) => Math.max(0, a - 1));
                }
              }}
              placeholder="Sök adress, syfte, kontakt …"
              aria-label="Sökterm"
              aria-controls="command-results"
              aria-activedescendant={options[active] ? `cmd-${active}` : undefined}
              className="h-12 flex-1 bg-transparent text-[15px] text-fg outline-none placeholder:text-fg-subtle"
            />
            <Kbd>Esc</Kbd>
          </form>
          <ul id="command-results" role="listbox" className="max-h-80 overflow-y-auto p-1.5">
            {options.map((o, i) => {
              const Icon = "icon" in o && o.icon ? o.icon : Search;
              return (
                <li key={o.key} id={`cmd-${i}`} role="option" aria-selected={i === active}>
                  <button
                    type="button"
                    onMouseEnter={() => setActive(i)}
                    onClick={() => go(o.href)}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm text-fg",
                      i === active && "bg-surface-2",
                    )}
                  >
                    <Icon className="size-4 text-fg-subtle" aria-hidden />
                    <span className="flex-1 truncate">{o.label}</span>
                    {i === active ? <CornerDownLeft className="size-3.5 text-fg-subtle" aria-hidden /> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </DialogContent>
      </Dialog>
    </>
  );
}
