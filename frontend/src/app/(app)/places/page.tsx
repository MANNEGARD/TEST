import type { Metadata } from "next";
import { Suspense } from "react";
import { PlacesView } from "@/components/places/places-view";

export const metadata: Metadata = { title: "Platser" };

export default function PlacesPage() {
  return (
    <Suspense>
      <PlacesView />
    </Suspense>
  );
}
