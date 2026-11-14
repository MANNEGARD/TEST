import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/placeholder-page";

export const metadata: Metadata = { title: "Bränsle & energi" };

export default function Page() {
  return <PlaceholderPage title="Bränsle & energi" description="Tankningar, laddningar och förbrukning." />;
}
