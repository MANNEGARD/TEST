"use client";

import { FlaskConical } from "lucide-react";
import { useVehicle } from "@/lib/vehicle";

/** Tydlig banderoll när ett demofordon är valt (Vehicle.is_demo). */
export function DemoBanner() {
  const { vehicle, vehicles, setVehicleId } = useVehicle();
  if (!vehicle?.is_demo) return null;
  const real = vehicles.find((v) => !v.is_demo);
  return (
    <div role="status" className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 border-b border-warn/40 bg-warn-soft px-4 py-2 text-center text-[13px] text-warn-ink">
      <span className="inline-flex items-center gap-1.5 font-semibold tracking-wide">
        <FlaskConical className="size-4" aria-hidden /> DEMOLÄGE
      </span>
      <span>Du tittar på ett demofordon med påhittade data. Inget här påverkar din riktiga körjournal.</span>
      {real ? (
        <button type="button" onClick={() => setVehicleId(real.id)} className="font-semibold underline underline-offset-2 hover:no-underline">
          Byt till {real.name}
        </button>
      ) : null}
    </div>
  );
}
