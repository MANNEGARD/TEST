import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/placeholder-page";

export const metadata: Metadata = { title: "Import" };

export default function Page() {
  return <PlaceholderPage title="Import" description="Importera körjournaler från Volvo Cars-appen." />;
}
