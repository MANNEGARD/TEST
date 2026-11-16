"use client";

import Link from "next/link";
import { Info, LogOut, Settings, User } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { sectionHref } from "@/components/settings/sections";
import { useAbout, versionLabel } from "@/lib/queries/about";
import { useLogout, useMe } from "@/lib/queries/auth";

export function UserMenu() {
  const { data } = useMe();
  const logout = useLogout();
  const about = useAbout();
  const version = about.data?.version;
  const user = data?.user;
  const initials = (user?.display_name || user?.email || "?")
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase())
    .join("");
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="inline-flex size-9 items-center justify-center rounded-full border border-border bg-surface-2 text-xs font-semibold text-fg-muted transition-colors hover:border-border-strong hover:text-fg focus-visible:outline-2 focus-visible:outline-ring"
        aria-label="Användarmeny"
      >
        {initials || <User className="size-4" />}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-56">
        <DropdownMenuLabel className="font-normal">
          <span className="block text-sm font-medium text-fg">{user?.display_name}</span>
          <span className="block truncate text-xs text-fg-muted">{user?.email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href="/settings">
            <Settings /> Inställningar
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => logout.mutate()}>
          <LogOut /> Logga ut
        </DropdownMenuItem>
        {version ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild className="text-xs text-fg-muted">
              <Link href={sectionHref("om")} data-testid="user-menu-version">
                <Info /> <span className="num">{versionLabel(version)}</span>
              </Link>
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
