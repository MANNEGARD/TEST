"use client";

import * as React from "react";
import { AlertTriangle, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FieldError, Input, Label } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { useDeleteHistory } from "@/lib/queries/settings-page";
import type { DeleteHistoryScope, SettingsPayload } from "@/lib/settings-page-types";
import { cn } from "@/lib/utils";
import { useVehicle } from "@/lib/vehicle";
import { useReportDirty } from "./dirty-context";
import { Advanced, BoolRow, Field, FormCard, Note, NumberRow, SectionIntro, SettingsSaveBar } from "./form-parts";
import { useSectionSave, useSettingsForm, type FieldSpecs } from "./use-settings-form";

function validUrl(value: string): string | null {
  const v = value.trim();
  if (!v) return "Ange en adress.";
  try {
    const u = new URL(v.replace(/\{[a-z]\}/g, "0"));
    return u.protocol === "https:" || u.protocol === "http:" ? null : "Adressen ska börja med https://.";
  } catch {
    return "Ange en fullständig adress, t.ex. https://…";
  }
}

const FIELDS: FieldSpecs = {
  "privacy.map_enabled": { type: "bool" },
  "privacy.map_tiles_url": {
    type: "text",
    name: "Kartserverns adress",
    maxLength: 500,
    validate: (v) => validUrl(v) ?? (/\{z\}/.test(v) && /\{x\}/.test(v) && /\{y\}/.test(v) ? null : "Adressen måste innehålla {z}, {x} och {y}."),
  },
  "privacy.geocoding_enabled": { type: "bool" },
  "privacy.geocoder_url": { type: "text", name: "Geokodarens adress", maxLength: 500, validate: validUrl },
  "privacy.observation_retention_days": { type: "number", name: "Gallring av rådata", min: 0, max: 3650, integer: true },
  "import.keep_original_files": { type: "bool" },
};

const SCOPES: { value: DeleteHistoryScope; title: string; description: string; perVehicle?: boolean }[] = [
  { value: "observations", title: "Rådata från bilen", description: "Positioner och mätvärden som resorna byggts av. Resorna finns kvar.", perVehicle: true },
  { value: "calendar", title: "Kalenderhändelser", description: "Hämtade möten och kopplingar till resor. Hämtas igen vid nästa synk om kalendern är kopplad." },
  { value: "exports", title: "Exporter", description: "Tidigare skapade exportfiler på servern." },
  { value: "notifications", title: "Notiser", description: "Historiken över skickade frågor och svar." },
  { value: "trips", title: "Resor", description: "Alla resor för det valda fordonet med klassningar, syften och granskningar. Hela körjournalen försvinner.", perVehicle: true },
];

const REMOVED_LABELS: Record<string, [string, string]> = {
  observations: ["observation", "observationer"],
  calendar_events: ["kalenderhändelse", "kalenderhändelser"],
  exports: ["export", "exporter"],
  notifications: ["notis", "notiser"],
  trips: ["resa", "resor"],
};

function DeleteHistoryDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { vehicle } = useVehicle();
  const del = useDeleteHistory();
  const [scope, setScope] = React.useState<DeleteHistoryScope | null>(null);
  const [confirm, setConfirm] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const selected = SCOPES.find((s) => s.value === scope);
  const ready = !!selected && confirm === "RADERA" && (!selected.perVehicle || !!vehicle);

  const close = (next: boolean) => {
    if (del.isPending) return;
    onOpenChange(next);
    if (!next) {
      setScope(null);
      setConfirm("");
      setError(null);
    }
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready || !selected) return;
    setError(null);
    del.mutate(
      { scope: selected.value, vehicle_id: selected.perVehicle ? vehicle?.id : undefined, confirm },
      {
        onSuccess: (res) => {
          const parts = Object.entries(res.removed).map(([k, n]) => {
            const [one, many] = REMOVED_LABELS[k] ?? [k, k];
            return `${formatNumber(n, 0)} ${n === 1 ? one : many}`;
          });
          toast.success(parts.length ? `Raderat: ${parts.join(", ")}.` : "Historiken är raderad.");
          close(false);
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-xl">
        <form onSubmit={submit} noValidate className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Radera historik</DialogTitle>
            <DialogDescription>Det du raderar tas bort permanent och går inte att ångra. Gör gärna en säkerhetskopia först.</DialogDescription>
          </DialogHeader>
          <fieldset>
            <legend className="text-[13px] font-medium text-fg">Vad vill du radera?</legend>
            <div className="mt-2 space-y-2">
              {SCOPES.map((s) => {
                const checked = scope === s.value;
                return (
                  <label
                    key={s.value}
                    className={cn(
                      "flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring",
                      checked ? "border-err/50 bg-err-soft/50" : "border-border hover:bg-surface-2/50",
                    )}
                  >
                    <input type="radio" name="delete-scope" value={s.value} checked={checked} onChange={() => setScope(s.value)} className="mt-1 accent-err" />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-fg">
                        {s.title}
                        {s.perVehicle && vehicle ? <span className="font-normal text-fg-muted"> – {vehicle.name}</span> : null}
                      </span>
                      <span className="mt-0.5 block text-xs leading-relaxed text-fg-muted">{s.description}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
          {selected?.perVehicle && !vehicle ? <Note tone="warn">Välj ett fordon i sidhuvudet först.</Note> : null}
          <div className="space-y-1.5">
            <Label htmlFor="delete-confirm" className="block">
              Skriv <span className="font-mono font-semibold">RADERA</span> för att bekräfta
            </Label>
            <Input
              id="delete-confirm"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              autoCapitalize="characters"
              className="font-mono"
            />
          </div>
          <FieldError>{error}</FieldError>
          <DialogFooter>
            <Button variant="ghost" onClick={() => close(false)} disabled={del.isPending}>
              Avbryt
            </Button>
            <Button type="submit" variant="danger" disabled={!ready} loading={del.isPending} data-testid="delete-history-submit">
              {del.isPending ? null : <Trash2 aria-hidden />}
              {selected ? `Radera ${selected.title.toLowerCase()}` : "Radera"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** 9. Sekretess – karta, geokodning, gallring och radering av historik. */
export function PrivacySection({ data }: { data: SettingsPayload }) {
  const form = useSettingsForm(data, FIELDS);
  const save = useSectionSave(form, "Sekretessinställningarna är sparade.");
  useReportDirty("sekretess", form.dirty);
  const [deleteOpen, setDeleteOpen] = React.useState(false);

  const mapOn = form.bool("privacy.map_enabled");
  const geoOn = form.bool("privacy.geocoding_enabled");
  const retention = form.number("privacy.observation_retention_days");

  return (
    <section aria-labelledby="settings-sekretess">
      <SectionIntro id="settings-sekretess" title="Sekretess">
        Allt sparas på din egen server. Här väljer du vad som får hämtas från eller skickas till externa tjänster och hur länge rådata
        sparas.
      </SectionIntro>

      <div className="space-y-4">
        <FormCard onSubmit={save.save} aria-label="Sekretess">
          <BoolRow
            form={form}
            path="privacy.map_enabled"
            id="priv-map"
            label="Visa kartor"
            hint="Kartbilderna hämtas från en kartserver, som då ser ungefär vilka områden du tittar på. Avstängt visas resorna utan karta."
          />
          {form.has("privacy.map_tiles_url") && mapOn ? (
            <div className="px-5 py-4">
              <Field
                id="priv-tiles"
                label="Kartserver"
                hint="Adress till kartbilderna med {z}, {x} och {y}. Standard är OpenStreetMap."
                error={form.error("privacy.map_tiles_url")}
              >
                <Input
                  id="priv-tiles"
                  value={form.text("privacy.map_tiles_url")}
                  onChange={(e) => form.set("privacy.map_tiles_url", e.target.value)}
                  aria-invalid={form.error("privacy.map_tiles_url") ? true : undefined}
                  aria-describedby="priv-tiles-hint"
                  className="font-mono text-xs"
                  spellCheck={false}
                  autoComplete="off"
                />
              </Field>
            </div>
          ) : null}
          <BoolRow
            form={form}
            path="privacy.geocoding_enabled"
            id="priv-geo"
            label="Slå upp adresser för nya platser"
            hint="Koordinaterna för nya platser skickas till en extern geokodningstjänst som svarar med gatuadress och ort. Av som standard – utan den namnger du platser själv."
          />
          {form.has("privacy.geocoder_url") && geoOn ? (
            <div className="px-5 py-4">
              <Field id="priv-geocoder" label="Geokodningstjänst" hint="Standard är Nominatim (OpenStreetMap)." error={form.error("privacy.geocoder_url")}>
                <Input
                  id="priv-geocoder"
                  value={form.text("privacy.geocoder_url")}
                  onChange={(e) => form.set("privacy.geocoder_url", e.target.value)}
                  aria-invalid={form.error("privacy.geocoder_url") ? true : undefined}
                  aria-describedby="priv-geocoder-hint"
                  className="font-mono text-xs"
                  spellCheck={false}
                  autoComplete="off"
                />
              </Field>
            </div>
          ) : null}
          <BoolRow
            form={form}
            path="import.keep_original_files"
            id="priv-keep-originals"
            label="Spara originalfilerna vid import"
            hint="Importerade filer (t.ex. Volvos reseexport) sparas på servern så att du kan hämta dem igen. Avstängt sparas bara resorna."
          />
          <Advanced summary="gallring av rådata" hasError={!!form.error("privacy.observation_retention_days")}>
            <NumberRow
              form={form}
              path="privacy.observation_retention_days"
              id="priv-retention"
              label="Radera rådata från bilen efter"
              unit="dagar"
              hint={
                retention === 0
                  ? "0 betyder att rådata sparas. Resorna påverkas aldrig – bara positioner och mätvärden de byggts av."
                  : "Positioner och mätvärden äldre än så raderas varje natt. Resorna och deras mätarställningar finns kvar."
              }
            />
          </Advanced>
          <SettingsSaveBar form={form} save={save} defaults />
        </FormCard>

        <Card className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <h3 className="flex items-center gap-2 text-sm font-semibold text-fg">
              <AlertTriangle className="size-4 text-err-ink" aria-hidden />
              Radera historik
            </h3>
            <p className="mt-1 max-w-md text-xs leading-relaxed text-fg-muted">
              Ta bort rådata, kalenderhändelser, exporter, notiser eller resor permanent. Det går inte att ångra.
            </p>
          </div>
          <Button variant="danger-ghost" onClick={() => setDeleteOpen(true)} data-testid="open-delete-history">
            <Trash2 aria-hidden /> Radera historik…
          </Button>
        </Card>
      </div>

      <DeleteHistoryDialog open={deleteOpen} onOpenChange={setDeleteOpen} />
    </section>
  );
}
