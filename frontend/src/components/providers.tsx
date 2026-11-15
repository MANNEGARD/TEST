"use client";

import { useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import { ThemeProvider, useTheme } from "@/lib/theme";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        // Försök inte igen vid klientfel (4xx) – de blir inte bättre av upprepning.
        retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
      },
      mutations: { retry: false },
    },
  });
}

function ThemedToaster() {
  const { resolved } = useTheme();
  return (
    <Toaster
      theme={resolved}
      // Överst i mitten: där skymmer kvittona varken sidopanelernas knappar (nere till höger)
      // eller mobilens flikrad och bottenark.
      position="top-center"
      closeButton
      offset={{ top: 68 }}
      mobileOffset={{ top: 64 }}
      toastOptions={{
        classNames: {
          toast: "!bg-surface !text-fg !border-border !shadow-lg !font-sans",
          description: "!text-fg-muted",
          actionButton: "!bg-accent !text-accent-fg !font-medium",
          cancelButton: "!bg-surface-2 !text-fg",
        },
      }}
    />
  );
}

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(makeQueryClient);
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>
        <TooltipProvider delayDuration={250} skipDelayDuration={100}>
          {children}
          <ThemedToaster />
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
