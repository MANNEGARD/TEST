"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Brain, MapPin, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { formatNumber } from "@/lib/format";
import { usePlaces } from "@/lib/queries/places";
import { MemoryTab } from "./memory-tab";
import { NewPlaceDialog } from "./new-place-dialog";
import { PlaceList } from "./place-list";
import { PlaceSheet } from "./place-sheet";
import { PlacesMap } from "./places-map";
import { Suggestions } from "./suggestions";

type TabKey = "platser" | "minne";

/**
 * Vyn Platser. Två flikar: "Platser" (förslag överst, därefter listan) och
 * "AI-minne" (inlärda mönster, regler och korrigeringar). Fliken ligger i URL:en
 * (?tab=minne) och en plats kan öppnas direkt med ?plats=<id>.
 */
export function PlacesView() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const tab: TabKey = params.get("tab") === "minne" ? "minne" : "platser";
  const [selected, setSelected] = React.useState<string | null>(() => params.get("plats"));
  const [newOpen, setNewOpen] = React.useState(false);
  const places = usePlaces();
  const list = places.data ?? [];

  const setTab = (next: string) => router.replace(next === "minne" ? `${pathname}?tab=minne` : pathname, { scroll: false });

  return (
    <>
      <PageHeader
        title="Platser"
        description="Adresserna du kör till. Ge dem namn och typ, så klassas och beskrivs dina tjänsteresor rätt av sig själva."
        actions={
          <Button variant="primary" onClick={() => setNewOpen(true)}>
            <Plus aria-hidden /> Ny plats
          </Button>
        }
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="platser">
            <MapPin className="size-4" aria-hidden /> Platser
            {places.data ? <span className="font-mono text-xs text-fg-subtle">{formatNumber(places.data.length, 0)}</span> : null}
          </TabsTrigger>
          <TabsTrigger value="minne">
            <Brain className="size-4" aria-hidden /> AI-minne
          </TabsTrigger>
        </TabsList>

        <TabsContent value="platser" className="pt-5">
          {places.isError ? (
            <Card>
              <ErrorState error={places.error} onRetry={() => places.refetch()} />
            </Card>
          ) : !places.data ? (
            <div className="space-y-3" role="status" aria-label="Laddar platser">
              <Skeleton className="h-9 max-w-xl" />
              <Card className="space-y-2 p-4">
                {Array.from({ length: 6 }, (_, i) => (
                  <Skeleton key={i} className="h-12" />
                ))}
              </Card>
            </div>
          ) : (
            <>
              <Suggestions onOpenPlace={setSelected} />
              <PlaceList places={list} onOpen={setSelected} onNew={() => setNewOpen(true)} />
              <PlacesMap places={list} />
            </>
          )}
        </TabsContent>

        <TabsContent value="minne" className="pt-5">
          <MemoryTab places={list} />
        </TabsContent>
      </Tabs>

      <PlaceSheet placeId={selected} places={list} onClose={() => setSelected(null)} onOpenPlace={setSelected} />
      <NewPlaceDialog open={newOpen} onOpenChange={setNewOpen} onCreated={(p) => setSelected(p.id)} />
    </>
  );
}
