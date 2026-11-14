import { Hammer } from "lucide-react";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

/** Tillfällig sida för vyer som byggs i nästa steg. */
export function PlaceholderPage({ title, description }: { title: string; description?: string }) {
  return (
    <>
      <PageHeader title={title} description={description} />
      <Card>
        <EmptyState icon={<Hammer />} title="Den här vyn byggs i nästa steg" description="Navigeringen fungerar redan – innehållet kommer snart." />
      </Card>
    </>
  );
}
