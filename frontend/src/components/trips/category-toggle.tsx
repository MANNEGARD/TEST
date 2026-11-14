"use client";

import { Briefcase, Home } from "lucide-react";
import { SegmentedControl } from "@/components/ui/segmented-control";
import type { CategoryCode } from "@/lib/types";

/**
 * Ett-klicks kategoribyte: Arbete / Privat. "Ej avgjord" visas som att inget
 * segment är valt. `onChange` anropas direkt (optimistisk uppdatering i hooken).
 */
export function CategoryToggle({
  value,
  onChange,
  size = "sm",
  disabled,
  tripLabel,
}: {
  value: string;
  onChange: (c: CategoryCode) => void;
  size?: "sm" | "md" | "lg";
  disabled?: boolean;
  tripLabel?: string;
}) {
  return (
    <SegmentedControl<CategoryCode>
      ariaLabel={tripLabel ? `Kategori för ${tripLabel}` : "Kategori"}
      value={value === "work" || value === "private" ? (value as CategoryCode) : undefined}
      onChange={(v) => v !== value && onChange(v)}
      size={size}
      disabled={disabled}
      options={[
        {
          value: "work",
          label: "Arbete",
          icon: <Briefcase aria-hidden />,
          activeClassName: "!bg-work-soft !text-work-ink ring-1 ring-work/40",
        },
        {
          value: "private",
          label: "Privat",
          icon: <Home aria-hidden />,
          activeClassName: "!bg-private-soft !text-private-ink ring-1 ring-private/40",
        },
      ]}
    />
  );
}
