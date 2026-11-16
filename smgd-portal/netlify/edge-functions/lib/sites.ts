// Subdomänerna som visas i portalen efter inloggning. Lägg till, ta bort eller ändra här.
// `icon` är namnet på en ikon i pages.ts.

export interface Site {
  id: string;
  name: string;
  url: string;
  description: string;
  icon: "code" | "car" | "home" | "eye" | "chip";
}

export const SITES: Site[] = [
  {
    id: "drivelog",
    name: "DriveLog",
    url: "https://drivelog.smgd.se",
    description: "Körjournal och milersättning",
    icon: "car",
  },
  {
    id: "ha",
    name: "Home Assistant",
    url: "https://ha.smgd.se",
    description: "Hemautomation",
    icon: "home",
  },
  {
    id: "vci",
    name: "VCI Config",
    url: "https://vci.smgd.se",
    description: "Konfiguration av VCI",
    icon: "chip",
  },
  {
    id: "dev",
    name: "Developer Lab",
    url: "https://dev.smgd.se",
    description: "Utveckling och experiment",
    icon: "code",
  },
];
