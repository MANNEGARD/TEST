"use client";

/**
 * Små åtgärdskomponenter för inställningssidan. Själva komponenterna ligger i
 * components/ui och delas med integrationsvyn.
 */

import * as React from "react";
import { ConfirmInline as BaseConfirmInline } from "@/components/ui/confirm-inline";

export { CopyButton } from "@/components/ui/copy-button";

/** Bekräftelse på plats – fyller bredden i inställningarnas listor. */
export function ConfirmInline(props: React.ComponentProps<typeof BaseConfirmInline>) {
  return <BaseConfirmInline {...props} className={props.className ?? "w-full"} />;
}
