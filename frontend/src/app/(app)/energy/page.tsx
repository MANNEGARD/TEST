import type { Metadata } from "next";
import { EnergyView } from "@/components/energy/energy-view";

export const metadata: Metadata = { title: "Bränsle & energi" };

export default function EnergyPage() {
  return <EnergyView />;
}
