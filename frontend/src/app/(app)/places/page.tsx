import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/placeholder-page";

export const metadata: Metadata = { title: "Platser" };

export default function Page() {
  return <PlaceholderPage title="Platser" description="Kända platser, förslag och regler per plats." />;
}
