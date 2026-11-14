import type { Metadata } from "next";
import { PlaceholderPage } from "@/components/placeholder-page";

export const metadata: Metadata = { title: "Snabbfråga" };

/** Snabbsvar på en notisfråga (länk från Home Assistant-notis). Byggs i nästa steg. */
export default async function Page({ params }: PageProps<"/m/q/[id]">) {
  await params;
  return <PlaceholderPage title="Snabbfråga" description="Besvara en fråga från en notis." />;
}
