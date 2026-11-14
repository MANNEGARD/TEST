import type { Metadata } from "next";
import { TripDetailView } from "@/components/trip-detail/trip-detail-view";

export const metadata: Metadata = { title: "Resa" };

export default async function TripPage({ params }: PageProps<"/journal/[id]">) {
  const { id } = await params;
  return <TripDetailView id={id} />;
}
