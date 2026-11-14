"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ErrorState } from "@/components/ui/error-state";
import { setTimeZone } from "@/lib/format";
import { useMe, useSetupStatus } from "@/lib/queries/auth";
import { useSettings } from "@/lib/queries/settings";
import { LogoMark } from "./logo";

function Splash() {
  return (
    <div className="flex min-h-dvh items-center justify-center" role="status" aria-label="Laddar DriveLog">
      <LogoMark className="size-10 animate-shimmer" />
    </div>
  );
}

/**
 * Skyddar inloggade sidor:
 *  1. Saknas konto → /setup.  2. Ej inloggad → /login?next=…
 *  3. Läser inställningarna och sätter tidszon för all formatering (lib/format).
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const setup = useSetupStatus();
  const me = useMe();
  const settings = useSettings(!!me.data);
  const [tzReady, setTzReady] = useState(false);

  useEffect(() => {
    if (setup.data?.needs_setup) router.replace("/setup");
    else if (me.isSuccess && me.data === null) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [setup.data, me.isSuccess, me.data, router, pathname]);

  useEffect(() => {
    if (settings.data) {
      setTimeZone(settings.data.settings.general?.timezone);
      setTzReady(true);
    } else if (settings.isError) {
      setTzReady(true); // fortsätt med standardtidszonen
    }
  }, [settings.data, settings.isError]);

  if (me.isError) return <ErrorState error={me.error} onRetry={() => me.refetch()} className="min-h-dvh" />;
  if (setup.data?.needs_setup || !me.data || !tzReady) return <Splash />;
  return <>{children}</>;
}
