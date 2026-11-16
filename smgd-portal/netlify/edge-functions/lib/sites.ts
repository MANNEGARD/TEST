// Subdomänerna som visas i portalen efter inloggning. Lägg till, ta bort eller ändra här.
// `logo` är en bild i public/logos/. Kortets accentfärg sätts i portal.css (.site[data-site="…"]).

export interface Site {
  id: string;
  name: string;
  url: string;
  description: string;
  logo: string;
}

export const SITES: Site[] = [
  {
    id: "drivelog",
    name: "DriveLog",
    url: "https://drivelog.smgd.se",
    description: "Körjournal och milersättning",
    logo: "/logos/drivelog.svg",
  },
  {
    id: "ha",
    name: "Home Assistant",
    url: "https://ha.smgd.se",
    description: "Hemautomation",
    logo: "/logos/ha.png",
  },
  {
    id: "vci",
    name: "VCI Config",
    url: "https://vci.smgd.se",
    description: "Konfiguration av VCI",
    logo: "/logos/vci.svg",
  },
  {
    id: "dev",
    name: "Developer Lab",
    url: "https://dev.smgd.se",
    description: "Utveckling och experiment",
    logo: "/logos/dev.svg",
  },
];
