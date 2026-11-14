"use client";

import * as React from "react";
import { CalendarRange, Check, ChevronDown } from "lucide-react";
import { formatDate } from "@/lib/format";
import { PERIOD_OPTIONS, periodLabel, periodToRange, type PeriodValue } from "@/lib/period";
import type { PeriodKey } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "./button";
import { Input, Label } from "./input";
import { Popover, PopoverContent, PopoverTrigger } from "./popover";

/**
 * Periodväljare: förvalda perioder som rader + anpassat intervall (från–till) i
 * foten. Värdet är `PeriodValue` ({ key, from?, to? }).
 */
export function DateRangePicker({
  value,
  onChange,
  options = PERIOD_OPTIONS.map((o) => o.key),
  className,
  align = "end",
}: {
  value: PeriodValue;
  onChange: (value: PeriodValue) => void;
  options?: PeriodKey[];
  className?: string;
  align?: "start" | "end";
}) {
  const [open, setOpen] = React.useState(false);
  const range = periodToRange(value);
  const [from, setFrom] = React.useState(value.from ?? range.from ?? "");
  const [to, setTo] = React.useState(value.to ?? range.to ?? "");
  const invalid = !!from && !!to && from > to;

  const summary =
    value.key === "custom" && value.from && value.to
      ? `${formatDate(value.from)} – ${formatDate(value.to)}`
      : periodLabel(value.key);

  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (o) {
          const r = periodToRange(value);
          setFrom(value.from ?? r.from ?? "");
          setTo(value.to ?? r.to ?? "");
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="secondary" size="md" className={cn("justify-between gap-2", className)} aria-label={`Period: ${summary}`}>
          <CalendarRange className="text-fg-subtle" aria-hidden />
          <span className="num">{summary}</span>
          <ChevronDown className="text-fg-subtle" aria-hidden />
        </Button>
      </PopoverTrigger>
      <PopoverContent align={align} className="w-64 p-1">
        <div role="listbox" aria-label="Välj period">
          {options
            .filter((k) => k !== "custom")
            .map((key) => {
              const selected = value.key === key;
              return (
                <button
                  key={key}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => {
                    onChange({ key });
                    setOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-center justify-between rounded-sm px-2.5 py-1.5 text-left text-sm hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-ring",
                    selected && "font-semibold",
                  )}
                >
                  {periodLabel(key)}
                  {selected ? <Check className="size-4 text-accent" strokeWidth={2.5} aria-hidden /> : null}
                </button>
              );
            })}
        </div>
        {options.includes("custom") ? (
          <form
            className="mt-1 space-y-2 border-t border-border p-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!from || !to || invalid) return;
              onChange({ key: "custom", from, to });
              setOpen(false);
            }}
          >
            <p className="text-xs font-medium text-fg-muted">Anpassat intervall</p>
            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1">
                <Label htmlFor="drp-from" className="text-xs text-fg-muted">
                  Från
                </Label>
                <Input id="drp-from" type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="h-8 px-2 text-xs" />
              </div>
              <div className="space-y-1">
                <Label htmlFor="drp-to" className="text-xs text-fg-muted">
                  Till
                </Label>
                <Input id="drp-to" type="date" value={to} onChange={(e) => setTo(e.target.value)} className="h-8 px-2 text-xs" />
              </div>
            </div>
            {invalid ? <p className="text-xs text-err-ink">Slutdatum ligger före startdatum.</p> : null}
            <Button type="submit" size="sm" variant="primary" className="w-full" disabled={!from || !to || invalid}>
              Använd intervall
            </Button>
          </form>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
