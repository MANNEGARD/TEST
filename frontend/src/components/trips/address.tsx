import { Tooltip } from "@/components/ui/tooltip";
import { MISSING } from "@/lib/format";
import type { LocationBrief } from "@/lib/types";

/** Adress/platsnamn: platsens namn om den är bekräftad, annars adressen. Hela adressen i tooltip. */
export function AddressCell({ address, location, className }: { address: string | null; location?: LocationBrief | null; className?: string }) {
  const name = location?.confirmed ? location.name : null;
  const primary = name ?? address;
  if (!primary) {
    return (
      <Tooltip content="Adress saknas">
        <span tabIndex={0} className="text-fg-subtle">
          {MISSING}
        </span>
      </Tooltip>
    );
  }
  return (
    <span className={className} title={address && address !== primary ? address : primary}>
      {primary}
    </span>
  );
}
