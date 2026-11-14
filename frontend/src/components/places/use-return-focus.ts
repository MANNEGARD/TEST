"use client";

import * as React from "react";

/**
 * Dialoger och sidopaneler som öppnas via state (utan Radix Trigger) lämnar inte
 * tillbaka fokus när de stängs. `remember()` anropas när panelen öppnas och
 * `restore` ges till innehållets `onCloseAutoFocus`.
 */
export function useReturnFocus() {
  const opener = React.useRef<HTMLElement | null>(null);
  return React.useMemo(
    () => ({
      remember: () => {
        opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      },
      restore: (event: Event) => {
        const el = opener.current;
        opener.current = null;
        if (el?.isConnected) {
          event.preventDefault();
          el.focus();
        }
      },
    }),
    [],
  );
}
