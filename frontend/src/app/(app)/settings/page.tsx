import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/placeholder-page";

export const metadata: Metadata = { title: "Inställningar" };

export default function Page() {
  return <PlaceholderPage title="Inställningar" description="Konto, skatteregler, sekretess och notiser." />;
}
