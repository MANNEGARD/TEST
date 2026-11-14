import {
  BedDouble,
  Building,
  Building2,
  CircleHelp,
  Fuel,
  Handshake,
  Home,
  MapPin,
  UserRound,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { label, LOCATION_TYPE_LABELS } from "@/lib/labels";
import type { LocationTypeCode } from "@/lib/places-types";
import { cn } from "@/lib/utils";

/** Platstyper i den ordning de visas i väljare (backend/app/models/enums.py → LocationType). */
export const LOCATION_TYPES: LocationTypeCode[] = [
  "home",
  "regular_workplace",
  "office",
  "customer",
  "hotel",
  "private",
  "fuel_station",
  "charging_station",
  "other",
  "unknown",
];

const ICONS: Record<string, LucideIcon> = {
  home: Home,
  regular_workplace: Building2,
  office: Building,
  customer: Handshake,
  hotel: BedDouble,
  private: UserRound,
  fuel_station: Fuel,
  charging_station: Zap,
  other: MapPin,
  unknown: CircleHelp,
};

export function typeLabel(type: string | null | undefined): string {
  return label(LOCATION_TYPE_LABELS, type);
}

export function LocationTypeIcon({ type, className }: { type: string; className?: string }) {
  const Icon = ICONS[type] ?? MapPin;
  return <Icon className={className} aria-hidden />;
}

/**
 * Rund ikonbricka för en plats. Obekräftade platser får streckad kant – markeringen
 * kombineras alltid med texten "Obekräftad" där brickan används.
 */
export function PlaceAvatar({ type, confirmed = true, className }: { type: string; confirmed?: boolean; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-full [&_svg]:size-4",
        confirmed ? "bg-accent-soft text-accent-ink" : "border border-dashed border-border-strong bg-surface-2 text-fg-subtle",
        className,
      )}
    >
      <LocationTypeIcon type={type} />
    </span>
  );
}

/** Väljare för platstyp med ikon + etikett. */
export function LocationTypeSelect({
  id,
  value,
  onChange,
  invalid,
}: {
  id?: string;
  value: string;
  onChange: (type: string) => void;
  invalid?: boolean;
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger id={id} aria-invalid={invalid || undefined}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {LOCATION_TYPES.map((t) => (
          <SelectItem key={t} value={t}>
            <span className="inline-flex items-center gap-2">
              <LocationTypeIcon type={t} className="size-4 shrink-0 text-fg-subtle" />
              {typeLabel(t)}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
