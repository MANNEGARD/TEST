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

/**
 * Content-Security-Policy för webbappen (gäller i drift: Caddy skickar huvudet vidare oförändrat).
 * Ett extra lager om en XSS någon gång skulle hittas: inga externa skript, ingen <base>, inga
 * <object>/<embed>, ingen inramning och formulär bara till samma ursprung.
 * - script-src 'unsafe-inline': Next lägger in inline-skript (och temaskriptet i layout.tsx).
 * - style-src 'unsafe-inline': Radix och sonner sätter stilar.
 * - img-src/connect-src https: http:: ägaren kan välja egen kartserver i inställningarna
 *   (http eller https), och MapLibres arbetare hämtar plattorna med fetch.
 * - worker-src 'self' blob:: MapLibres arbetare (public/vendor) och tjänstearbetaren sw.js.
 * Under `next dev` behöver React dessutom eval; det gäller aldrig bygget.
 * Demobygget (statiskt) får sin policy ur demo-static/_headers.
 */
const scriptSrc = process.env.NODE_ENV === "development" ? "'self' 'unsafe-inline' 'unsafe-eval'" : "'self' 'unsafe-inline'";
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  `script-src ${scriptSrc}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https: http:",
  "connect-src 'self' https: http:",
  "worker-src 'self' blob:",
  "font-src 'self' data:",
  "manifest-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

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
          { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
        ],
      },
    ];
  },
};

export default DEMO ? demoConfig : nextConfig;
