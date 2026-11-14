"use client";

import * as React from "react";
import type { SectionKey } from "./sections";

/**
 * Sektionerna rapporterar om de har osparade ändringar, så att navigeringen kan
 * markera dem och sidan kan varna innan fönstret stängs.
 */
export const DirtyContext = React.createContext<(key: SectionKey, dirty: boolean) => void>(() => {});

export function useReportDirty(key: SectionKey, dirty: boolean): void {
  const report = React.useContext(DirtyContext);
  React.useEffect(() => {
    report(key, dirty);
  }, [report, key, dirty]);
}
