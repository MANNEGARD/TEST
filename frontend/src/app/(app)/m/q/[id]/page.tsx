import type { Metadata } from "next";
import { QuickQuestionView } from "@/components/quick/quick-question-view";

/** Demobygget (statisk export): förbyggda sidor för demots resor och frågor. Annars ingen. */
export const generateStaticParams =
  process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1" ? async () => (await import("@/demo/static-params")).questionParams() : undefined;

export const metadata: Metadata = { title: "Snabbfråga" };

/** Snabbsvar på en notisfråga (länk från Home Assistant-notisen på telefonen). */
export default async function Page({ params }: PageProps<"/m/q/[id]">) {
  const { id } = await params;
  return <QuickQuestionView publicId={id} />;
}
