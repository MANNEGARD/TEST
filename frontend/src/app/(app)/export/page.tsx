import type { Metadata } from "next";
import { Suspense } from "react";
import { ExportView } from "@/components/export/export-view";

export const metadata: Metadata = { title: "Export" };

export default function ExportPage() {
  return (
    <Suspense>
      <ExportView />
    </Suspense>
  );
}
