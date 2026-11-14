"use client";

import { AppShell } from "@/components/shell/app-shell";
import { AuthGate } from "@/components/shell/auth-gate";
import { VehicleProvider } from "@/lib/vehicle";

/** Alla inloggade sidor: autentisering → fordonskontext → app-skal. */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <VehicleProvider>
        <AppShell>{children}</AppShell>
      </VehicleProvider>
    </AuthGate>
  );
}
