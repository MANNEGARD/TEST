import type { Metadata } from "next";
import { MonthReviewView } from "@/components/quick/month-review";

/** Demobygget (statisk export): förbyggda sidor för demots resor och frågor. Annars ingen. */
export const generateStaticParams =
  process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1" ? async () => (await import("@/demo/static-params")).monthParams() : undefined;

export const metadata: Metadata = { title: "Månadens genomgång" };

/** Månadens antagna och automatiskt godkända rader (länken "Se antagna" i månadsnotisen). */
export default async function Page({ params }: PageProps<"/m/month/[month]">) {
  const { month } = await params;
  return <MonthReviewView month={month} />;
}
