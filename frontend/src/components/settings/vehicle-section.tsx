"use client";

import * as React from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { useMe } from "@/lib/queries/auth";
import { useDrivers, useSaveSettings, useUpdateVehicle } from "@/lib/queries/settings-page";
import type { SettingsChanges, SettingsPayload, VehiclePatch } from "@/lib/settings-page-types";
import type { Vehicle } from "@/lib/types";
import { useVehicle } from "@/lib/vehicle";
import { Car } from "lucide-react";
import { useReportDirty } from "./dirty-context";
import { Field, FormCard, GroupHeading, SaveBar, SectionIntro } from "./form-parts";
import { OdometerReadings } from "./odometer-readings";
import { useSettingsForm } from "./use-settings-form";

type VehicleField = "name" | "make" | "model" | "model_year" | "registration_number";

/** Gränserna följer backend/app/api/routes/vehicles.py → VehicleIn. */
const MAX = { name: 120, make: 80, model: 120, registration_number: 20 } as const;
const YEAR_MIN = 1950;
const YEAR_MAX = 2100;

function serverValues(v: Vehicle): Record<VehicleField, string> {
  return {
    name: v.name,
    make: v.make ?? "",
    model: v.model ?? "",
    model_year: v.model_year != null ? String(v.model_year) : "",
    registration_number: v.registration_number ?? "",
  };
}

function normalize(field: VehicleField, value: string): string {
  const t = value.trim();
  return field === "registration_number" ? t.toUpperCase() : t;
}

function validate(values: Record<VehicleField, string>): Partial<Record<VehicleField, string>> {
  const errors: Partial<Record<VehicleField, string>> = {};
  if (!values.name.trim()) errors.name = "Ange ett namn på fordonet.";
  for (const f of ["name", "make", "model", "registration_number"] as const) {
    if (values[f].trim().length > MAX[f]) errors[f] = `Högst ${MAX[f]} tecken.`;
  }
  const year = values.model_year.trim();
  if (year && (!/^\d{4}$/.test(year) || Number(year) < YEAR_MIN || Number(year) > YEAR_MAX)) {
    errors.model_year = `Ange ett årtal mellan ${YEAR_MIN} och ${YEAR_MAX}.`;
  }
  return errors;
}

function toPatch(fields: VehicleField[], values: Record<VehicleField, string>): VehiclePatch {
  const patch: VehiclePatch = {};
  for (const f of fields) {
    const v = normalize(f, values[f]);
    if (f === "model_year") patch.model_year = v ? Number(v) : null;
    else if (f === "registration_number") patch.registration_number = v || null;
    else patch[f] = v;
  }
  return patch;
}

const GENERAL_FIELDS = {
  "general.driver_name": { type: "text", name: "Förarens namn", maxLength: 200 },
  "general.employer_name": { type: "text", name: "Arbetsgivarens namn", maxLength: 200 },
} as const;

function VehicleForm({ vehicle, data }: { vehicle: Vehicle; data: SettingsPayload }) {
  const me = useMe();
  const drivers = useDrivers();
  const updateVehicle = useUpdateVehicle();
  const saveSettings = useSaveSettings();
  const form = useSettingsForm(data, GENERAL_FIELDS);

  const server = serverValues(vehicle);
  const [draft, setDraft] = React.useState<Partial<Record<VehicleField, string>>>({});
  const [failure, setFailure] = React.useState<string | null>(null);
  const values = { ...server, ...draft };
  const changed = (Object.keys(draft) as VehicleField[]).filter((f) => normalize(f, values[f]) !== normalize(f, server[f]));
  const errors = validate(values);
  const vehicleDirty = changed.length > 0;
  const dirty = vehicleDirty || form.dirty;
  const invalid = (Object.values(errors)[0] as string | undefined) ?? form.firstError;
  const saving = updateVehicle.isPending || saveSettings.isPending;
  useReportDirty("fordon", dirty);

  const set = (field: VehicleField) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setFailure(null);
    setDraft((d) => ({ ...d, [field]: e.target.value }));
  };
  const setGeneral = (path: keyof typeof GENERAL_FIELDS) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setFailure(null);
    form.set(path, e.target.value);
  };

  // Samma ordning som exporten: eget namn → standardföraren → kontots namn.
  const defaultDriver = drivers.data?.find((d) => d.is_default) ?? drivers.data?.[0];
  const fallbackDriver = defaultDriver?.name || me.data?.user.display_name || "";

  const run = async (vehiclePatch: VehiclePatch | null, settings: SettingsChanges | null): Promise<string | null> => {
    const jobs: Promise<unknown>[] = [];
    if (vehiclePatch) jobs.push(updateVehicle.mutateAsync({ id: vehicle.id, patch: vehiclePatch }).then(() => setDraft({})));
    if (settings) jobs.push(saveSettings.mutateAsync(settings).then(() => form.reset()));
    const failed = (await Promise.allSettled(jobs)).find((r) => r.status === "rejected");
    return failed ? errorMessage((failed as PromiseRejectedResult).reason) : null;
  };

  const save = async () => {
    if (!dirty || invalid || saving) return;
    setFailure(null);
    const before = { vehicle: vehicleDirty ? toPatch(changed, server) : null, settings: form.dirty ? form.previous : null };
    const problem = await run(vehicleDirty ? toPatch(changed, values) : null, form.dirty ? form.changes : null);
    if (problem) {
      setFailure(problem);
      toast.error(problem);
      return;
    }
    toast.success("Fordon och förare är sparade.", {
      action: {
        label: "Ångra",
        onClick: () =>
          void run(before.vehicle, before.settings).then((undoProblem) =>
            undoProblem ? toast.error(undoProblem) : toast.success("Ändringen är ångrad."),
          ),
      },
    });
  };

  return (
    <FormCard onSubmit={() => void save()} aria-label="Fordon och förare">
      <GroupHeading>
        <span className="flex flex-wrap items-center gap-2">
          Fordon
          {vehicle.is_demo ? (
            <Badge tone="warn" size="sm">
              Demobil
            </Badge>
          ) : null}
        </span>
      </GroupHeading>
      <div className="grid gap-x-4 gap-y-4 px-5 py-4 sm:grid-cols-6">
        <Field id="veh-name" label="Namn" hint="Så heter bilen i appen, t.ex. i fordonsväljaren." error={errors.name} className="sm:col-span-4">
          <Input id="veh-name" value={values.name} onChange={set("name")} maxLength={MAX.name} aria-invalid={errors.name ? true : undefined} autoComplete="off" />
        </Field>
        <Field id="veh-reg" label="Registreringsnummer" error={errors.registration_number} className="sm:col-span-2">
          <Input
            id="veh-reg"
            value={values.registration_number}
            onChange={set("registration_number")}
            maxLength={MAX.registration_number}
            placeholder="ABC123"
            className="font-mono uppercase placeholder:normal-case"
            aria-invalid={errors.registration_number ? true : undefined}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
          />
        </Field>
        <Field id="veh-make" label="Märke" error={errors.make} className="sm:col-span-2">
          <Input id="veh-make" value={values.make} onChange={set("make")} maxLength={MAX.make} placeholder="t.ex. Volvo" autoComplete="off" />
        </Field>
        <Field id="veh-model" label="Modell" error={errors.model} className="sm:col-span-2">
          <Input id="veh-model" value={values.model} onChange={set("model")} maxLength={MAX.model} placeholder="t.ex. V90" autoComplete="off" />
        </Field>
        <Field id="veh-year" label="Årsmodell" error={errors.model_year} className="sm:col-span-2">
          <Input
            id="veh-year"
            value={values.model_year}
            onChange={set("model_year")}
            inputMode="numeric"
            maxLength={4}
            placeholder="ÅÅÅÅ"
            className="font-mono"
            aria-invalid={errors.model_year ? true : undefined}
            autoComplete="off"
          />
        </Field>
      </div>

      <GroupHeading description="Namnen står i sidhuvudet på den exporterade körjournalen.">Förare och arbetsgivare</GroupHeading>
      <div className="grid gap-4 px-5 py-4 sm:grid-cols-2">
        <Field
          id="gen-driver"
          label="Förarens namn"
          hint={fallbackDriver ? `Lämnas fältet tomt används ${fallbackDriver}.` : undefined}
          error={form.error("general.driver_name")}
        >
          <Input
            id="gen-driver"
            value={form.text("general.driver_name")}
            onChange={setGeneral("general.driver_name")}
            placeholder={fallbackDriver || "För- och efternamn"}
            autoComplete="name"
          />
        </Field>
        <Field id="gen-employer" label="Arbetsgivare" hint="Företaget du redovisar tjänsteresorna till." error={form.error("general.employer_name")}>
          <Input
            id="gen-employer"
            value={form.text("general.employer_name")}
            onChange={setGeneral("general.employer_name")}
            placeholder="t.ex. Exempelbolaget AB"
            autoComplete="organization"
          />
        </Field>
      </div>

      <SaveBar
        dirty={dirty}
        invalid={invalid}
        error={failure}
        saving={saving}
        onReset={() => {
          setDraft({});
          form.reset();
          setFailure(null);
        }}
      />
    </FormCard>
  );
}

/** 1. Fordon och förare – uppgifterna som hamnar i exportens sidhuvud. */
export function VehicleSection({ data }: { data: SettingsPayload }) {
  const { vehicle, isLoading } = useVehicle();
  return (
    <section aria-labelledby="settings-fordon">
      <SectionIntro id="settings-fordon" title="Fordon och förare">
        Uppgifterna om bilen och dig som förare. De används i sidhuvudet när du exporterar körjournalen till din arbetsgivare.
      </SectionIntro>
      {vehicle ? (
        <div className="space-y-4">
          <VehicleForm key={vehicle.id} vehicle={vehicle} data={data} />
          <OdometerReadings key={`odo-${vehicle.id}`} vehicle={vehicle} />
        </div>
      ) : isLoading ? (
        <Card className="p-5">
          <SkeletonBlock rows={5} />
        </Card>
      ) : (
        <Card>
          <EmptyState icon={<Car />} title="Inget fordon hittades" description="Välj ett fordon i sidhuvudet och försök igen." compact />
        </Card>
      )}
    </section>
  );
}
