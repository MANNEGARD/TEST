import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/placeholder-page";

export const metadata: Metadata = { title: "Export" };

export default function Page() {
  return <PlaceholderPage title="Export" description="Exportera körjournalen för arbetsgivare och Skatteverket." />;
}
