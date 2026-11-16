import type { NextConfig } from "next";

/**
 * Webbläsaren talar alltid med samma origin: /api/v1/* proxas vidare till backend
 * (FastAPI). I Docker pekar API_INTERNAL_URL på api-tjänsten, lokalt på :8000.
 */
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

/**
 * Demoflaggan bakas alltid in (tom utan demo), så att demokrokarna viks bort vid bygget och
 * demokoden inte följer med i det vanliga bygget.
 */
const DEMO = process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1";
const env = { NEXT_PUBLIC_DRIVELOG_DEMO: DEMO ? "1" : "" };

const turbopack: NextConfig["turbopack"] = {
  rules: {
    "*.css": {
      loaders: ["@tailwindcss/turbopack"],
      as: "*.css",
    },
  },
};

/**
 * Demobygget (`npm run build:demo`, NEXT_PUBLIC_DRIVELOG_DEMO=1): statiska filer utan server.
 * Servern simuleras i webbläsaren (src/demo/). Inga rewrites, inga serverfunktioner och ingen
 * service worker. Bara .tsx räknas som sidor, så att hälsokontrollen (route.ts) och manifestet
 * inte följer med. scripts/finish-demo.mjs flyttar resultatet till out-demo/.
 */
const demoConfig: NextConfig = {
  output: "export",
  distDir: ".next-demo",
  trailingSlash: true,
  pageExtensions: ["tsx"],
  images: { unoptimized: true },
  poweredByHeader: false,
  reactStrictMode: true,
  devIndicators: false,
  env,
  turbopack,
};

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  devIndicators: false,
  env,
  experimental: {
    // Uppladdningar (import, kvitton, backup) passerar proxyn – standard är 10 MB.
    // Backend avgör den verkliga gränsen och svarar 413 med svensk text.
    proxyClientMaxBodySize: "25mb",
  },
  turbopack,
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${apiInternalUrl}/api/v1/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default DEMO ? demoConfig : nextConfig;
