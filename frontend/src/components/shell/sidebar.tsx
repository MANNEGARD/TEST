"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useOpenReviewCount } from "@/lib/queries/review";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Logo } from "./logo";
import { isActive, NAV_GROUP_LABELS, NAV_ITEMS, type NavItem } from "./nav-items";

export function NavBadge({ count, className }: { count: number | undefined; className?: string }) {
  if (!count) return null;
  return (
    <span
      className={cn(
        "ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-warn-soft px-1.5 text-[11px] font-semibold text-warn-ink num",
        className,
      )}
      aria-label={`${count} öppna ärenden`}
    >
      {count > 99 ? "99+" : formatNumber(count, 0)}
    </span>
  );
}

export function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const reviewCount = useOpenReviewCount();
  const groups = (["main", "system", "more"] as NavItem["group"][]).map((g) => ({
    group: g,
    items: NAV_ITEMS.filter((i) => i.group === g),
  }));
  return (
    <nav aria-label="Huvudmeny" className="flex flex-col gap-5">
      {groups.map(({ group, items }) => (
        <div key={group}>
          {NAV_GROUP_LABELS[group] ? (
            <p className="mb-1 px-2.5 text-[11px] font-semibold tracking-wider text-fg-subtle uppercase">{NAV_GROUP_LABELS[group]}</p>
          ) : null}
          <ul className="flex flex-col gap-0.5">
            {items.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "group flex h-9 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium text-fg-muted transition-colors hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-ring",
                      active && "bg-surface-2 text-fg shadow-[inset_2px_0_0_var(--accent)]",
                    )}
                  >
                    <item.icon className={cn("size-4 shrink-0 text-fg-subtle group-hover:text-fg-muted", active && "text-accent")} aria-hidden />
                    <span className="truncate">{item.label}</span>
                    {item.badge === "review" ? <NavBadge count={reviewCount} /> : null}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

export function Sidebar() {
  return (
    <aside className="hidden w-60 shrink-0 border-r border-border bg-surface lg:block">
      <div className="sticky top-0 flex h-dvh flex-col">
      <div className="flex h-14 items-center border-b border-border px-4">
        <Link href="/" className="rounded-md focus-visible:outline-2 focus-visible:outline-ring" aria-label="SMGD DriveLog – Översikt">
          <Logo />
        </Link>
      </div>
      <div className="scrollbar-thin flex-1 overflow-y-auto px-3 py-4">
        <NavList />
      </div>
      <div className="border-t border-border px-4 py-3 text-[11px] leading-relaxed text-fg-subtle">
        Självhostad körjournal.
        <br />
        Dina uppgifter lämnar aldrig servern.
      </div>
      </div>
    </aside>
  );
}
