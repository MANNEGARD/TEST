"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { formatNumber } from "@/lib/format";
import { Button } from "./button";

export function Pagination({
  page,
  pageSize,
  total,
  onPage,
  className,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
  className?: string;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <nav aria-label="Sidnumrering" className={`flex items-center justify-between gap-3 text-sm ${className ?? ""}`}>
      <p className="text-fg-muted num">
        {formatNumber(from, 0)}–{formatNumber(to, 0)} av {formatNumber(total, 0)}
      </p>
      <div className="flex items-center gap-1">
        <Button size="icon-sm" variant="ghost" onClick={() => onPage(page - 1)} disabled={page <= 1} aria-label="Föregående sida">
          <ChevronLeft />
        </Button>
        <span className="min-w-16 text-center text-fg-muted num" aria-current="page">
          {page} / {pages}
        </span>
        <Button size="icon-sm" variant="ghost" onClick={() => onPage(page + 1)} disabled={page >= pages} aria-label="Nästa sida">
          <ChevronRight />
        </Button>
      </div>
    </nav>
  );
}
