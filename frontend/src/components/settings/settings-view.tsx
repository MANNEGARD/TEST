"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettingsPage } from "@/lib/queries/settings-page";
import type { SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { AbbreviationsSection } from "./abbreviations-section";
import { AiSection } from "./ai-section";
import { AutomationSection } from "./automation-section";
import { BackupSection } from "./backup-section";
import { CalendarSection } from "./calendar-section";
import { DirtyContext } from "./dirty-context";
import { LunchSection } from "./lunch-section";
import { MergeSection } from "./merge-section";
import { NotificationsSection } from "./notifications-section";
import { PrivacySection } from "./privacy-section";
import { ReimbursementSection } from "./reimbursement-section";
import { DEFAULT_SECTION, isSectionKey, SECTION_GROUP_LABELS, sectionHref, SECTIONS, type SectionKey } from "./sections";
import { SecuritySection } from "./security-section";
import { VehicleSection } from "./vehicle-section";
import { WorkplaceSection } from "./workplace-section";

const PANELS: Record<SectionKey, React.ComponentType<{ data: SettingsPayload }>> = {
  fordon: VehicleSection,
  ersattning: ReimbursementSection,
  sammanslagning: MergeSection,
  automatik: AutomationSection,
  lunch: LunchSection,
  forslag: AiSection,
  notiser: NotificationsSection,
  kalender: CalendarSection,
  tjanstestalle: WorkplaceSection,
  forkortningar: AbbreviationsSection,
  sekretess: PrivacySection,
  sakerhet: SecuritySection,
  sakerhetskopia: BackupSection,
};

const DESKTOP = "(min-width: 1024px)";

function subscribeDesktop(onChange: () => void): () => void {
  const mq = window.matchMedia(DESKTOP);
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}

/** Sant på breda skärmar, där sektionerna ligger i en lodrät meny (styr piltangenternas riktning). */
function useIsDesktop(): boolean {
  return React.useSyncExternalStore(
    subscribeDesktop,
    () => window.matchMedia(DESKTOP).matches,
    () => false,
  );
}

/**
 * Inställningar. Sektionerna väljs i en meny till vänster (flikar på mobil) och den
 * valda sektionen ligger i URL:en (?sektion=notiser). En sektion laddas första gången
 * den öppnas och ligger sedan kvar, så osparade ändringar finns kvar när man bläddrar
 * mellan sektionerna – de markeras med en prick i menyn.
 */
export function SettingsView() {
  const router = useRouter();
  const params = useSearchParams();
  const requested = params.get("sektion");
  const active: SectionKey = isSectionKey(requested) ? requested : DEFAULT_SECTION;
  const settings = useSettingsPage();
  const isDesktop = useIsDesktop();

  const [visited, setVisited] = React.useState<ReadonlySet<SectionKey>>(() => new Set([active]));
  if (!visited.has(active)) setVisited(new Set(visited).add(active));

  const [dirty, setDirty] = React.useState<Partial<Record<SectionKey, boolean>>>({});
  const reportDirty = React.useCallback(
    (key: SectionKey, value: boolean) => setDirty((prev) => (!!prev[key] === value ? prev : { ...prev, [key]: value })),
    [],
  );
  const anyDirty = Object.values(dirty).some(Boolean);

  // Webbläsarens egen varning om fönstret stängs eller laddas om med osparade ändringar.
  React.useEffect(() => {
    if (!anyDirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDirty]);

  // Håll den valda fliken synlig i den vågräta fliklisten på mobil.
  const listRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    const list = listRef.current;
    const tab = list?.querySelector<HTMLElement>('[role="tab"][data-state="active"]');
    if (!list || !tab || list.scrollWidth <= list.clientWidth) return;
    list.scrollTo({ left: tab.offsetLeft - (list.clientWidth - tab.offsetWidth) / 2, behavior: "smooth" });
  }, [active]);

  const select = (key: string) => {
    if (!isSectionKey(key) || key === active) return;
    router.replace(sectionHref(key), { scroll: false });
    // Börja läsa den nya sektionen uppifrån.
    if (window.scrollY > 120) window.scrollTo({ top: 0 });
  };

  return (
    <>
      <PageHeader title="Inställningar" description="Det vanligaste står först. Resten har rimliga standardvärden och kan lämnas som det är." />

      <DirtyContext.Provider value={reportDirty}>
        <TabsPrimitive.Root
          value={active}
          onValueChange={select}
          orientation={isDesktop ? "vertical" : "horizontal"}
          className="lg:grid lg:grid-cols-[232px_minmax(0,1fr)] lg:items-start lg:gap-10"
        >
          <div className="sticky top-14 z-10 -mx-4 mb-5 border-b border-border bg-bg/95 backdrop-blur sm:-mx-6 lg:top-20 lg:z-0 lg:mx-0 lg:mb-0 lg:border-0 lg:bg-transparent lg:backdrop-blur-none">
            <TabsPrimitive.List
              ref={listRef}
              aria-label="Sektioner"
              className="no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-2.5 sm:px-6 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:p-0"
            >
              {SECTIONS.map((s, i) => {
                const firstInGroup = i === 0 || SECTIONS[i - 1].group !== s.group;
                return (
                  <React.Fragment key={s.key}>
                    {firstInGroup ? (
                      <span
                        role="presentation"
                        className={cn(
                          "hidden px-2.5 pb-1 text-[11px] font-semibold tracking-wider text-fg-subtle uppercase lg:block",
                          i > 0 && "lg:mt-5",
                        )}
                      >
                        {SECTION_GROUP_LABELS[s.group]}
                      </span>
                    ) : null}
                    <TabsPrimitive.Trigger
                      value={s.key}
                      data-testid={`settings-tab-${s.key}`}
                      className={cn(
                        // mobil: piller i en vågrät rad
                        "group inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1.5 text-[13px] font-medium whitespace-nowrap text-fg-muted transition-colors hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                        "data-[state=active]:border-fg data-[state=active]:bg-fg data-[state=active]:text-bg",
                        // desktop: rader i en lodrät meny (samma uttryck som huvudmenyn)
                        "lg:flex lg:h-9 lg:w-full lg:gap-2.5 lg:rounded-md lg:border-0 lg:bg-transparent lg:px-2.5 lg:py-0 lg:text-left lg:text-sm lg:hover:bg-surface-2",
                        "lg:data-[state=active]:bg-surface-2 lg:data-[state=active]:text-fg lg:data-[state=active]:shadow-[inset_2px_0_0_var(--accent)]",
                      )}
                    >
                      <s.icon
                        className="hidden size-4 shrink-0 text-fg-subtle group-data-[state=active]:text-accent lg:block"
                        aria-hidden
                      />
                      <span className="lg:hidden">{s.short}</span>
                      <span className="hidden truncate lg:inline">{s.label}</span>
                      {dirty[s.key] ? (
                        <span className="flex items-center lg:ml-auto">
                          <span className="size-1.5 rounded-full bg-warn" aria-hidden />
                          <span className="sr-only">(osparade ändringar)</span>
                        </span>
                      ) : null}
                    </TabsPrimitive.Trigger>
                  </React.Fragment>
                );
              })}
            </TabsPrimitive.List>
          </div>

          <div className="min-w-0 lg:max-w-3xl">
            {settings.isError && !settings.data ? (
              <Card>
                <ErrorState error={settings.error} onRetry={() => settings.refetch()} title="Det gick inte att hämta inställningarna" />
              </Card>
            ) : !settings.data ? (
              <div role="status" aria-label="Laddar inställningar">
                <Skeleton className="mb-2 h-6 w-56" />
                <Skeleton className="mb-5 h-4 w-80 max-w-full" />
                <Card className="space-y-6 p-5">
                  {Array.from({ length: 4 }, (_, i) => (
                    <div key={i} className="flex items-start justify-between gap-8">
                      <div className="flex-1 space-y-2">
                        <Skeleton className="h-4 w-40" />
                        <Skeleton className="h-3 w-72 max-w-full" />
                      </div>
                      <Skeleton className="h-9 w-28" />
                    </div>
                  ))}
                </Card>
                <span className="sr-only">Laddar…</span>
              </div>
            ) : (
              SECTIONS.filter((s) => visited.has(s.key)).map((s) => {
                const Panel = PANELS[s.key];
                return (
                  <TabsPrimitive.Content
                    key={s.key}
                    value={s.key}
                    forceMount
                    data-testid={`settings-panel-${s.key}`}
                    className="focus-visible:outline-none data-[state=inactive]:hidden"
                  >
                    <Panel data={settings.data} />
                  </TabsPrimitive.Content>
                );
              })
            )}
          </div>
        </TabsPrimitive.Root>
      </DirtyContext.Provider>
    </>
  );
}
