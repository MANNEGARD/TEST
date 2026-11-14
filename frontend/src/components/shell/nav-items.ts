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
  group: "main" | "data" | "system";
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Översikt", icon: LayoutDashboard, mobile: true, group: "main" },
  { href: "/journal", label: "Körjournal", icon: NotebookTabs, mobile: true, group: "main" },
  { href: "/review", label: "Granskning", icon: ClipboardCheck, mobile: true, badge: "review", group: "main" },
  { href: "/places", label: "Platser", icon: MapPin, group: "main" },
  { href: "/energy", label: "Bränsle & energi", icon: Fuel, mobile: true, group: "main" },
  { href: "/import", label: "Import", icon: Upload, group: "data" },
  { href: "/export", label: "Export", icon: Download, group: "data" },
  { href: "/integrations", label: "Integrationer", icon: PlugZap, group: "system" },
  { href: "/settings", label: "Inställningar", icon: Settings, group: "system" },
];

export const NAV_GROUP_LABELS: Record<NavItem["group"], string | null> = {
  main: null,
  data: "Data",
  system: "System",
};

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
