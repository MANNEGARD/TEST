import type { Metadata } from "next";
import { MonthReviewView } from "@/components/quick/month-review";

export const metadata: Metadata = { title: "Månadens genomgång" };

/** Månadens antagna och automatiskt godkända rader (länken "Se antagna" i månadsnotisen). */
export default async function Page({ params }: PageProps<"/m/month/[month]">) {
  const { month } = await params;
  return <MonthReviewView month={month} />;
}
