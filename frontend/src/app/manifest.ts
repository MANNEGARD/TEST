import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "SMGD DriveLog",
    short_name: "DriveLog",
    description: "Självhostad, intelligent körjournal.",
    lang: "sv",
    dir: "ltr",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "any",
    background_color: "#0e1116",
    theme_color: "#0e1116",
    categories: ["productivity", "business", "utilities"],
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
    shortcuts: [
      { name: "Körjournal", url: "/journal" },
      { name: "Granskning", url: "/review" },
    ],
  };
}
