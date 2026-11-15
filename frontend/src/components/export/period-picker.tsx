"use client";

import * as React from "react";
import { CalendarRange, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FieldError, Input, Label } from "@/components/ui/input";
import {
  currentMonth,
  defaultMonth,
  isValidRange,
  monthLabel,
  periodRange,
  shiftMonth,
  type ExportPeriod,
} from "./export-period";

/**
 * Periodval: månadsstegare "‹ Juni 2026 ›" och ett diskret "Annan period" med två
 * datumfält. Piltangenterna vänster/höger stegar när stegaren har fokus.
 */
export function PeriodPicker({ value, onChange }: { value: ExportPeriod; onChange: (p: ExportPeriod) => void }) {
  const max = currentMonth();
  const custom = value.kind === "custom";
  const month = value.kind === "month" ? value.month : value.from.slice(0, 7);
  const canNext = !custom && month < max;

  // URL:en uppdateras asynkront – snabba tryck i följd ska ändå stega en månad var.
  const latest = React.useRef(month);
  React.useEffect(() => {
    latest.current = month;
  }, [month]);

  const step = (delta: number) => {
    if (custom) return;
    const next = shiftMonth(latest.current, delta);
    if (next > max) return;
    latest.current = next;
    onChange({ kind: "month", month: next });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      step(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      step(1);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {custom ? (
          <CustomRange value={value} onChange={onChange} />
        ) : (
          <div
            role="group"
            aria-label="Välj månad. Piltangenterna vänster och höger byter månad."
            onKeyDown={onKeyDown}
            className="inline-flex items-center rounded-lg border border-border bg-surface p-1 shadow-sm"
          >
            <Button variant="ghost" size="icon-sm" onClick={() => step(-1)} aria-label={`Föregående månad (${monthLabel(shiftMonth(month, -1))})`}>
              <ChevronLeft aria-hidden />
            </Button>
            <output
              aria-live="polite"
              className="min-w-[9.5rem] px-2 text-center text-[15px] font-semibold tracking-tight text-fg tabular-nums"
            >
              {monthLabel(month)}
            </output>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => step(1)}
              disabled={!canNext}
              aria-label={canNext ? `Nästa månad (${monthLabel(shiftMonth(month, 1))})` : "Nästa månad (ligger i framtiden)"}
            >
              <ChevronRight aria-hidden />
            </Button>
          </div>
        )}
        {custom ? (
          <Button variant="link" size="sm" className="text-fg-muted hover:text-fg" onClick={() => onChange({ kind: "month", month: value.from.slice(0, 7) <= max ? value.from.slice(0, 7) : defaultMonth() })}>
            Välj månad i stället
          </Button>
        ) : (
          <Button
            variant="link"
            size="sm"
            className="text-fg-muted hover:text-fg"
            onClick={() => onChange({ kind: "custom", ...periodRange(value) })}
          >
            <CalendarRange aria-hidden /> Annan period
          </Button>
        )}
    </div>
  );
}

function CustomRange({ value, onChange }: { value: Extract<ExportPeriod, { kind: "custom" }>; onChange: (p: ExportPeriod) => void }) {
  const [from, setFrom] = React.useState(value.from);
  const [to, setTo] = React.useState(value.to);
  // Följ URL:en om perioden ändras utifrån (t.ex. bakåtknappen).
  const [synced, setSynced] = React.useState(value);
  if (synced.from !== value.from || synced.to !== value.to) {
    setSynced(value);
    setFrom(value.from);
    setTo(value.to);
  }

  const commit = (f: string, t: string) => {
    if (isValidRange(f, t) && f >= "2000-01-01" && (f !== value.from || t !== value.to)) onChange({ kind: "custom", from: f, to: t });
  };
  const invalid = !!from && !!to && from > to;

  return (
    <div role="group" aria-label="Annan period" className="flex flex-wrap items-end gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor="export-from" className="text-xs text-fg-muted">
          Från
        </Label>
        <Input
          id="export-from"
          type="date"
          value={from}
          max={to || undefined}
          aria-invalid={invalid || undefined}
          onChange={(e) => {
            setFrom(e.target.value);
            commit(e.target.value, to);
          }}
          className="w-[10.5rem]"
        />
      </div>
      <span className="pb-2 text-fg-subtle" aria-hidden>
        –
      </span>
      <div className="flex flex-col gap-1">
        <Label htmlFor="export-to" className="text-xs text-fg-muted">
          Till
        </Label>
        <Input
          id="export-to"
          type="date"
          value={to}
          min={from || undefined}
          aria-invalid={invalid || undefined}
          onChange={(e) => {
            setTo(e.target.value);
            commit(from, e.target.value);
          }}
          className="w-[10.5rem]"
        />
      </div>
      {invalid ? <FieldError className="basis-full">Tilldatum får inte ligga före fråndatum.</FieldError> : null}
    </div>
  );
}
