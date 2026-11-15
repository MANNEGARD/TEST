import type { Metadata } from "next";
import { QuickQuestionView } from "@/components/quick/quick-question-view";

export const metadata: Metadata = { title: "Snabbfråga" };

/** Snabbsvar på en notisfråga (länk från Home Assistant-notisen på telefonen). */
export default async function Page({ params }: PageProps<"/m/q/[id]">) {
  const { id } = await params;
  return <QuickQuestionView publicId={id} />;
}
