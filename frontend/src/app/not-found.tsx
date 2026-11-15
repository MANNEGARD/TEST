import type { Metadata } from "next";
import Link from "next/link";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Sidan hittades inte" };

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="num text-sm font-medium text-fg-subtle">404</p>
      <h1 className="text-xl font-semibold">Sidan hittades inte</h1>
      <p className="text-sm text-fg-muted">Länken kan vara felaktig eller sidan flyttad.</p>
      <Button asChild variant="primary" className="mt-2">
        <Link href="/">Till översikten</Link>
      </Button>
    </div>
  );
}
