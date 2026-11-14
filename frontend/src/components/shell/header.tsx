"use client";

import Link from "next/link";
import { LogoMark } from "./logo";
import { CommandSearch } from "./command-search";
import { ThemeToggle } from "./theme-toggle";
import { UserMenu } from "./user-menu";
import { VehicleSwitcher } from "./vehicle-switcher";

export function Header() {
  return (
    <header className="sticky top-0 z-20 border-b border-border bg-bg/85 backdrop-blur supports-[backdrop-filter]:bg-bg/75">
      <div className="mx-auto flex h-14 max-w-[1440px] items-center gap-2 px-4 sm:gap-3 sm:px-6">
        <Link href="/" className="rounded-md focus-visible:outline-2 focus-visible:outline-ring lg:hidden" aria-label="Översikt">
          <LogoMark />
        </Link>
        <VehicleSwitcher />
        <div className="flex flex-1 justify-end md:justify-center">
          <CommandSearch />
        </div>
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
