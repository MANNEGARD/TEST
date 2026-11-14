"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { MoreHorizontal } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useOpenReviewCount } from "@/lib/queries/review";
import { cn } from "@/lib/utils";
import { isActive, NAV_ITEMS } from "./nav-items";
import { NavList } from "./sidebar";

/** Bottennavigering på mobil: fyra viktigaste + "Mer" (ark med hela menyn). */
export function MobileNav() {
  const pathname = usePathname();
  const reviewCount = useOpenReviewCount();
  const [open, setOpen] = React.useState(false);
  const items = NAV_ITEMS.filter((i) => i.mobile).slice(0, 4);
  const moreActive = !items.some((i) => isActive(pathname, i.href));

  return (
    <>
      <nav
        aria-label="Snabbmeny"
        className="pb-safe fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/85 lg:hidden"
      >
        <ul className="grid grid-cols-5">
          {items.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "relative flex h-14 flex-col items-center justify-center gap-1 text-[11px] font-medium text-fg-muted focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
                    active && "text-accent-ink",
                  )}
                >
                  <span className="relative">
                    <item.icon className="size-5" aria-hidden />
                    {item.badge === "review" && reviewCount ? (
                      <span className="absolute -top-1.5 -right-2.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-warn px-1 text-[10px] font-bold text-black num">
                        {reviewCount > 99 ? "99+" : reviewCount}
                      </span>
                    ) : null}
                  </span>
                  <span className="max-w-full truncate px-1">{item.label}</span>
                </Link>
              </li>
            );
          })}
          <li>
            <button
              type="button"
              onClick={() => setOpen(true)}
              aria-haspopup="dialog"
              aria-expanded={open}
              className={cn(
                "flex h-14 w-full flex-col items-center justify-center gap-1 text-[11px] font-medium text-fg-muted focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
                moreActive && "text-accent-ink",
              )}
            >
              <MoreHorizontal className="size-5" aria-hidden />
              Mer
            </button>
          </li>
        </ul>
      </nav>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="bottom">
          <SheetHeader>
            <SheetTitle>Meny</SheetTitle>
            <SheetDescription className="sr-only">Alla sidor i DriveLog</SheetDescription>
          </SheetHeader>
          <NavList onNavigate={() => setOpen(false)} />
        </SheetContent>
      </Sheet>
    </>
  );
}
