"use client";

import { Sparkles } from "lucide-react";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useAbout, versionLabel } from "@/lib/queries/about";
import { Note, SectionIntro } from "./form-parts";

/** Ändringsloggen i projektets repo (samma adress som `url` i drivelog/config.yaml) – gäller alla driftsätt. */
const CHANGELOG_URL = "https://github.com/SMGD-MP/TEST/blob/HEAD/drivelog/CHANGELOG.md";

/** Punkterna kommer ur ändringsloggen; ta bort eventuell markdown (fetstil, kod) så att texten blir ren. */
function plain(text: string): string {
  return text.replace(/\*\*|__|`/g, "").trim();
}

/**
 * Om DriveLog – vilken version som körs och vad som är nytt i den. Samma text som notisen
 * "DriveLog är uppdaterad till …" använder (GET /about). Ingenting här går att ändra.
 */
export function AboutSection() {
  const about = useAbout();
  const info = about.data;
  const isDemo = info?.version === "demo";
  const items = (info?.whats_new?.items ?? []).map(plain).filter(Boolean);

  return (
    <section aria-labelledby="settings-om">
      <SectionIntro id="settings-om" title="Om DriveLog">
        Vilken version av DriveLog som körs på din server och vad som är nytt i den.
      </SectionIntro>

      {about.isError && !info ? (
        <Card>
          <ErrorState error={about.error} onRetry={() => about.refetch()} title="Det gick inte att hämta versionen" />
        </Card>
      ) : !info ? (
        <Card className="space-y-3 p-5" role="status" aria-label="Laddar versionen">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-4 w-72 max-w-full" />
          <Skeleton className="h-4 w-64 max-w-full" />
          <span className="sr-only">Laddar…</span>
        </Card>
      ) : (
        <Card className="divide-y divide-border" data-testid="about-card">
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 px-5 py-4">
            <p className="text-[13px] font-medium text-fg">Version</p>
            <p className="text-sm font-semibold text-fg num" data-testid="about-version">
              {isDemo ? versionLabel(info.version) : info.version}
            </p>
          </div>

          {isDemo ? (
            <div className="px-5 py-4">
              <p className="text-[13px] leading-relaxed text-fg-muted">
                Det här är demot av DriveLog. På din egen server står versionsnumret här, till exempel 1.3.0, tillsammans med det viktigaste som är nytt i den
                versionen.
              </p>
            </div>
          ) : (
            <div className="px-5 py-4">
              <h3 className="flex items-center gap-2 text-[13px] font-medium text-fg">
                <Sparkles className="size-4 text-accent" aria-hidden />
                Nytt i {info.whats_new?.version ?? info.version}
              </h3>
              {items.length ? (
                <ul className="mt-2 list-disc space-y-1.5 pl-5 text-[13px] leading-relaxed text-fg-muted marker:text-fg-subtle" data-testid="about-whats-new">
                  {items.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1.5 text-[13px] leading-relaxed text-fg-muted">Ändringsloggen för den här versionen följer inte med på den här servern.</p>
              )}
            </div>
          )}

          <div className="px-5 py-4">
            <Note>
              <p data-testid="about-changelog">
                Hela ändringsloggen finns på{" "}
                <a
                  href={CHANGELOG_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring"
                >
                  GitHub
                </a>
                {" "}– och, om du kör DriveLog som tillägg, på tilläggets sida i Home Assistant.
              </p>
            </Note>
          </div>
        </Card>
      )}
    </section>
  );
}
