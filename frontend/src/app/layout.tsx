import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { GeistMono } from "geist/font/mono";
import { DemoBannerSlot } from "@/components/demo-slot";
import { Providers } from "@/components/providers";
import { ServiceWorkerRegistration } from "@/components/sw-register";
import { DEMO_CLOCK_SCRIPT } from "@/lib/demo-hook";
import { THEME_INIT_SCRIPT } from "@/lib/theme";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "SMGD DriveLog", template: "%s · DriveLog" },
  description: "Självhostad, intelligent körjournal.",
  applicationName: "SMGD DriveLog",
  appleWebApp: { capable: true, title: "DriveLog", statusBarStyle: "default" },
  formatDetection: { telephone: false },
  icons: {
    icon: [
      { url: "/icons/icon.svg", type: "image/svg+xml" },
      { url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
    ],
    apple: [{ url: "/icons/apple-touch-icon.png", sizes: "180x180" }],
  },
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f7f7f5" },
    { media: "(prefers-color-scheme: dark)", color: "#0e1116" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="sv" className={`${GeistSans.variable} ${GeistMono.variable}`} suppressHydrationWarning>
      <head>
        {DEMO_CLOCK_SCRIPT ? <script dangerouslySetInnerHTML={{ __html: DEMO_CLOCK_SCRIPT }} /> : null}
        {/* Sätter tema före första målningen (undviker blink). */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <meta name="mobile-web-app-capable" content="yes" />
      </head>
      <body className="min-h-dvh antialiased">
        {DemoBannerSlot ? <DemoBannerSlot /> : null}
        <Providers>{children}</Providers>
        <ServiceWorkerRegistration />
      </body>
    </html>
  );
}
