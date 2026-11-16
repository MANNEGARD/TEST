import type { Metadata } from "next";
import { TripDetailView } from "@/components/trip-detail/trip-detail-view";

/** Demobygget (statisk export): förbyggda sidor för demots resor och frågor. Annars ingen. */
export const generateStaticParams =
  process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1" ? async () => (await import("@/demo/static-params")).journalParams() : undefined;

export const metadata: Metadata = { title: "Resa" };

export default async function TripPage({ params }: PageProps<"/journal/[id]">) {
  const { id } = await params;
  return <TripDetailView id={id} />;
}
