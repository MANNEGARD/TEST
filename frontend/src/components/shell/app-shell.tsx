"use client";

import type { ReactNode } from "react";
import { DemoBanner } from "./demo-banner";
import { HaBanner } from "./ha-banner";
import { Header } from "./header";
import { MobileNav } from "./mobile-nav";
import { Sidebar } from "./sidebar";

/** App-skalet: fast sidomeny (desktop), sidhuvud, innehåll och bottennavigering (mobil). */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-accent px-3 py-2 text-accent-fg focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Hoppa till innehållet
      </a>
      <Sidebar />
      <div className="flex min-h-dvh min-w-0 flex-1 flex-col">
        <DemoBanner />
        <HaBanner />
        <Header />
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1440px] flex-1 px-4 pt-6 pb-28 outline-none sm:px-6 lg:pb-12">
          {children}
        </main>
      </div>
      <MobileNav />
    </div>
  );
}
