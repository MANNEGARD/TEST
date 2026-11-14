"use client";

/**
 * Valt fordon (fordonsväljaren i sidhuvudet). Valet sparas i localStorage.
 * Alla fordonsberoende queries ska skicka `vehicleId` som `vehicle_id` och ha det
 * i sin query-nyckel.
 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useVehicles } from "./queries/vehicles";
import type { Vehicle } from "./types";
import { readStorage, writeStorage } from "./utils";

const STORAGE_KEY = "drivelog.vehicle";

interface VehicleContextValue {
  vehicles: Vehicle[];
  vehicle: Vehicle | undefined;
  vehicleId: string | undefined;
  setVehicleId: (id: string) => void;
  isLoading: boolean;
}

const VehicleContext = createContext<VehicleContextValue | null>(null);

export function VehicleProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { data, isLoading } = useVehicles();
  const [selected, setSelected] = useState<string | undefined>(() =>
    typeof window === "undefined" ? undefined : readStorage<string | undefined>("local", STORAGE_KEY, undefined),
  );

  const vehicles = useMemo(() => data ?? [], [data]);
  const vehicle = useMemo(() => {
    const byId = vehicles.find((v) => v.id === selected);
    return byId ?? vehicles.find((v) => v.is_default) ?? vehicles.find((v) => !v.is_demo) ?? vehicles[0];
  }, [vehicles, selected]);

  const setVehicleId = useCallback(
    (id: string) => {
      setSelected(id);
      writeStorage("local", STORAGE_KEY, id);
      // Fordonsberoende data hämtas om för det nya fordonet.
      void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "auth" && q.queryKey[0] !== "settings" });
    },
    [qc],
  );

  const value = useMemo(
    () => ({ vehicles, vehicle, vehicleId: vehicle?.id, setVehicleId, isLoading }),
    [vehicles, vehicle, setVehicleId, isLoading],
  );
  return <VehicleContext.Provider value={value}>{children}</VehicleContext.Provider>;
}

export function useVehicle(): VehicleContextValue {
  const ctx = useContext(VehicleContext);
  if (!ctx) throw new Error("useVehicle måste användas inom <VehicleProvider>");
  return ctx;
}
