"use client";

import * as React from "react";
import { Building2, Home, MapPin, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError, Input, Label } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useAddWorkplace, useDeleteWorkplace, useLocationOptions, useWorkplaces } from "@/lib/queries/settings-page";
import type { SettingsPayload, Workplace, WorkplaceInput } from "@/lib/settings-page-types";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, FormCard, Note, SectionIntro, SettingsSaveBar } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

const FIELDS: FieldSpecs = {
  "tax.home_is_workplace": { type: "bool" },
  "tax.commute_rule_enabled": { type: "bool" },
  "tax.car_benefit": { type: "bool" },
  "tax.require_approval_when_uncertain": { type: "bool" },
};

function period(wp: Workplace): string {
  if (wp.valid_from && wp.valid_to) return `${formatDate(wp.valid_from)} – ${formatDate(wp.valid_to)}`;
  if (wp.valid_from) return `Från ${formatDate(wp.valid_from)}`;
  if (wp.valid_to) return `Till och med ${formatDate(wp.valid_to)}`;
  return "Gäller tills vidare";
}

function toInput(wp: Workplace): WorkplaceInput {
  return { kind: wp.kind, location_id: wp.location_id, valid_from: wp.valid_from, valid_to: wp.valid_to, note: wp.note };
}

function AddWorkplace() {
  const locations = useLocationOptions();
  const add = useAddWorkplace();
  const [kind, setKind] = React.useState<"location" | "home">("location");
  const [locationId, setLocationId] = React.useState("");
  const [from, setFrom] = React.useState("");
  const [to, setTo] = React.useState("");
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (kind === "location" && !locationId) return setError("Välj vilken plats som är tjänsteställe.");
    if (from && to && to < from) return setError("Slutdatum ligger före startdatum.");
    setError(null);
    add.mutate(
      { kind, location_id: kind === "location" ? locationId : null, valid_from: from || null, valid_to: to || null, note: note.trim() },
      {
        onSuccess: () => {
          toast.success("Tjänstestället är tillagt. Förslagen för oavgjorda resor räknas om.");
          setLocationId("");
          setFrom("");
          setTo("");
          setNote("");
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} noValidate className="space-y-3 px-5 py-4" aria-label="Lägg till tjänsteställe">
      <h3 className="text-[13px] font-medium text-fg">Lägg till tjänsteställe</h3>
      <SegmentedControl
        ariaLabel="Typ av tjänsteställe"
        value={kind}
        onChange={(v) => {
          setKind(v);
          setError(null);
        }}
        options={[
          { value: "location", label: "En plats", icon: <MapPin aria-hidden /> },
          { value: "home", label: "Bostaden", icon: <Home aria-hidden /> },
        ]}
      />
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_9.5rem_9.5rem]">
        {kind === "location" ? (
          <div className="space-y-1.5">
            <Label htmlFor="wp-location" className="block">
              Plats
            </Label>
            <Select value={locationId} onValueChange={setLocationId} disabled={!locations.data}>
              <SelectTrigger id="wp-location">
                <SelectValue placeholder={locations.isError ? "Platserna gick inte att hämta" : locations.data ? "Välj plats" : "Hämtar platser…"} />
              </SelectTrigger>
              <SelectContent>
                {(locations.data ?? []).map((l) => (
                  <SelectItem key={l.id} value={l.id}>
                    {l.name}
                    {l.city && !l.name.includes(l.city) ? <span className="text-fg-subtle"> · {l.city}</span> : null}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <p className="self-end pb-2 text-[13px] text-fg-muted">Bostaden räknas som tjänsteställe under perioden.</p>
        )}
        <div className="space-y-1.5">
          <Label htmlFor="wp-from" className="block">
            Från <span className="font-normal text-fg-subtle">(valfritt)</span>
          </Label>
          <Input id="wp-from" type="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="wp-to" className="block">
            Till <span className="font-normal text-fg-subtle">(valfritt)</span>
          </Label>
          <Input id="wp-to" type="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} />
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
        <div className="space-y-1.5">
          <Label htmlFor="wp-note" className="block">
            Anteckning <span className="font-normal text-fg-subtle">(valfritt)</span>
          </Label>
          <Input id="wp-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} autoComplete="off" placeholder="t.ex. kontoret i Jönköping" />
        </div>
        <Button type="submit" variant="secondary" loading={add.isPending}>
          {add.isPending ? null : <Plus aria-hidden />} Lägg till
        </Button>
      </div>
      <FieldError>{error}</FieldError>
    </form>
  );
}

function WorkplaceRow({ wp }: { wp: Workplace }) {
  const remove = useDeleteWorkplace();
  const add = useAddWorkplace();
  const name = wp.location_name ?? (wp.kind === "home" ? "Bostaden" : "Okänd plats");
  const onDelete = () =>
    remove.mutate(wp.id, {
      onSuccess: () =>
        toast.success(`${name} är borttaget som tjänsteställe.`, {
          action: {
            label: "Ångra",
            onClick: () => add.mutate(toInput(wp), { onError: (err) => toast.error(errorMessage(err)) }),
          },
        }),
      onError: (err) => toast.error(errorMessage(err)),
    });

  return (
    <li className="flex items-center gap-3 px-5 py-3">
      <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-surface-2 text-fg-subtle [&_svg]:size-4" aria-hidden>
        {wp.kind === "home" ? <Home /> : <Building2 />}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-fg">{name}</p>
        <p className="text-xs text-fg-muted">
          {period(wp)}
          {wp.note ? ` · ${wp.note}` : ""}
        </p>
      </div>
      <Button variant="ghost" size="icon-sm" onClick={onDelete} loading={remove.isPending} aria-label={`Ta bort tjänstestället ${name}`}>
        {remove.isPending ? null : <Trash2 aria-hidden />}
      </Button>
    </li>
  );
}

/** 7. Tjänsteställe – skatteregler som bara spelar roll i vissa fall. */
export function WorkplaceSection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS);
  const save = useSectionSave(form, "Skattereglerna är sparade. Förslagen för oavgjorda resor räknas om.");
  useReportDirty("tjanstestalle", form.dirty);
  const workplaces = useWorkplaces();

  return (
    <section aria-labelledby="settings-tjanstestalle">
      <SectionIntro id="settings-tjanstestalle" title="Tjänsteställe" badge="Avancerat – skatteregler">
        Resor mellan bostaden och ditt tjänsteställe räknas som privata enligt Skatteverket och ger ingen
        milersättning. Ange ett tjänsteställe om du vill att förslagen tar hänsyn till det.
      </SectionIntro>

      <div className="space-y-4">
        <Note>
          Behöver du inte bry dig om det här? Då kan du hoppa över sektionen. Utan tjänsteställe bygger förslagen på platser och dina
          tidigare val, och du avgör alltid själv vad som är tjänsteresa.
        </Note>

        <Card>
          {workplaces.isError ? (
            <ErrorState compact error={workplaces.error} onRetry={() => workplaces.refetch()} title="Tjänsteställena gick inte att hämta" />
          ) : !workplaces.data ? (
            <div className="px-5 py-4">
              <SkeletonBlock rows={2} />
            </div>
          ) : workplaces.data.length === 0 ? (
            <EmptyState
              compact
              icon={<Building2 />}
              title="Inget tjänsteställe angivet"
              description="Lägg till kontoret eller arbetsplatsen du normalt utgår från. Byter du arbetsplats anger du en period för varje."
            />
          ) : (
            <ul className="divide-y divide-border" aria-label="Tjänsteställen">
              {workplaces.data.map((wp) => (
                <WorkplaceRow key={wp.id} wp={wp} />
              ))}
            </ul>
          )}
          <div className="border-t border-border">
            <AddWorkplace />
          </div>
        </Card>

        <FormCard onSubmit={save.save} aria-label="Skatteregler">
          <BoolRow
            form={form}
            path="tax.home_is_workplace"
            id="tax-home"
            label="Bostaden är tjänsteställe"
            hint="Gäller om du huvudsakligen arbetar hemifrån. Då räknas resor från bostaden till kunder och andra arbetsplatser som tjänsteresor."
          />
          <BoolRow
            form={form}
            path="tax.commute_rule_enabled"
            id="tax-commute"
            label="Föreslå resor till och från tjänstestället som privata"
            hint="Skatteverkets regel för resor mellan bostad och arbete. Du kan alltid ändra en enskild resa."
          />
          {form.has("tax.car_benefit") || form.has("tax.require_approval_when_uncertain") ? (
            <Advanced summary="skatteregler">
              <BoolRow
                form={form}
                path="tax.car_benefit"
                id="tax-car-benefit"
                label="Förmånsbil (bilen ägs av arbetsgivaren)"
                hint="Med förmånsbil ska all körning redovisas – välj då även ”Fullständig körjournal” under Milersättning och export."
              />
              <BoolRow
                form={form}
                path="tax.require_approval_when_uncertain"
                id="tax-require-approval"
                label="Kräv alltid mitt godkännande vid skattemässig osäkerhet"
                hint="Resor där skattereglerna inte ger ett tydligt svar föreslås aldrig automatiskt utan väntar på ditt beslut."
              />
            </Advanced>
          ) : null}
          <SettingsSaveBar form={form} save={save} defaults />
        </FormCard>
      </div>
    </section>
  );
}
