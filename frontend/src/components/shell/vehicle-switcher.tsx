"use client";

import { Car, Check, ChevronsUpDown, FlaskConical } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { useVehicle } from "@/lib/vehicle";
import type { Vehicle } from "@/lib/types";

function vehicleSubtitle(v: Vehicle): string {
  return [v.registration_number, [v.make, v.model].filter(Boolean).join(" ")].filter(Boolean).join(" · ") || (v.is_demo ? "Demofordon" : "Fordon");
}

export function VehicleSwitcher() {
  const { vehicles, vehicle, setVehicleId, isLoading } = useVehicle();
  if (isLoading || !vehicle) return <Skeleton className="h-9 w-40" />;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex h-9 max-w-[220px] min-w-0 items-center gap-2 rounded-md border border-border bg-surface px-2.5 text-left shadow-sm transition-colors hover:border-border-strong focus-visible:outline-2 focus-visible:outline-ring"
        aria-label={`Fordon: ${vehicle.name}. Byt fordon`}
      >
        {vehicle.is_demo ? <FlaskConical className="size-4 shrink-0 text-warn-ink" aria-hidden /> : <Car className="size-4 shrink-0 text-fg-subtle" aria-hidden />}
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{vehicle.name}</span>
        <ChevronsUpDown className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-64">
        <DropdownMenuLabel>Fordon</DropdownMenuLabel>
        {vehicles.map((v) => (
          <DropdownMenuItem key={v.id} onSelect={() => setVehicleId(v.id)} className="py-2">
            {v.is_demo ? <FlaskConical className="!text-warn-ink" /> : <Car />}
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate font-medium">{v.name}</span>
              <span className="truncate text-xs text-fg-muted">{vehicleSubtitle(v)}</span>
            </span>
            {v.id === vehicle.id ? <Check className="!text-accent" aria-label="Valt" /> : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
