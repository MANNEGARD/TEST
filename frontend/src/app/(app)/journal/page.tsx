import type { Metadata } from "next";
import { Suspense } from "react";
import { JournalView } from "@/components/journal/journal-view";

export const metadata: Metadata = { title: "Körjournal" };

export default function JournalPage() {
  return (
    <Suspense>
      <JournalView />
    </Suspense>
  );
}
