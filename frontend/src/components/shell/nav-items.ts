import {
  ClipboardCheck,
  Download,
  Fuel,
  LayoutDashboard,
  MapPin,
  NotebookTabs,
  PlugZap,
  Settings,
  Upload,
  type LucideIcon,
} from "lucide-react";

/**
 * Navigeringen. Lägg till nya sidor här (sidomeny + "Mer" på mobil).
 * `mobile: true` = visas i bottennavigeringen (högst fyra).
 * `badge: "review"` = visar antal öppna granskningsärenden.
 */
export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  mobile?: boolean;
  badge?: "review";
  group: "main" | "system" | "more";
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Översikt", icon: LayoutDashboard, mobile: true, group: "main" },
  { href: "/journal", label: "Körjournal", icon: NotebookTabs, mobile: true, group: "main" },
  { href: "/review", label: "Granskning", icon: ClipboardCheck, mobile: true, badge: "review", group: "main" },
  { href: "/export", label: "Export", icon: Download, mobile: true, group: "main" },
  { href: "/places", label: "Platser", icon: MapPin, group: "main" },
  { href: "/integrations", label: "Integrationer", icon: PlugZap, group: "system" },
  { href: "/settings", label: "Inställningar", icon: Settings, group: "system" },
  { href: "/import", label: "Import", icon: Upload, group: "more" },
  { href: "/energy", label: "Bränsle & energi", icon: Fuel, group: "more" },
];

export const NAV_GROUP_LABELS: Record<NavItem["group"], string | null> = {
  main: null,
  system: "System",
  more: "Mer",
};

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
