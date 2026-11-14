"use client";

import { Columns3 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { DEFAULT_VISIBLE, TRIP_COLUMNS } from "@/components/trips/trip-columns";

export function ColumnPicker({ visible, onChange }: { visible: string[]; onChange: (v: string[]) => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="secondary" size="icon" aria-label="Välj kolumner" className="hidden md:inline-flex">
          <Columns3 />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-52">
        <DropdownMenuLabel>Visa kolumner</DropdownMenuLabel>
        {TRIP_COLUMNS.map((c) => (
          <DropdownMenuCheckboxItem
            key={c.id}
            checked={visible.includes(c.id)}
            disabled={c.required}
            onSelect={(e) => e.preventDefault()}
            onCheckedChange={(on) => onChange(on ? [...visible, c.id] : visible.filter((v) => v !== c.id))}
          >
            {c.label}
          </DropdownMenuCheckboxItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onChange(DEFAULT_VISIBLE)}>Återställ standard</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
