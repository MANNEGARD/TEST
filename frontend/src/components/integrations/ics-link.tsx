"use client";

/**
 * Kalenderlänk (ICS): en hemlig länk till en publicerad Outlook-kalender, som alternativ
 * till appregistreringen. Länken är en hemlighet – servern sparar den krypterad och lämnar
 * aldrig ut den, så fältet förifylls aldrig. Servern hämtar länken innan den sparas och
 * svarar med klartext på svenska när något är fel.
 */

import * as React from "react";
import { Info, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { FieldError } from "@/components/ui/input";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatDateTime, plural } from "@/lib/format";
import { useIcsStatus, useIcsSync, useRemoveIcsLink, useSaveIcsLink } from "@/lib/queries/integrations";
import { ConfirmButton, LastErrorNote, Note, notifyError, Section, SecretField } from "./shared";

/** "Synka nu" för kalenderlänken med kvitto – används både på kortet och i panelen. */
export function useIcsSyncAction() {
  const sync = useIcsSync();
  return {
    isPending: sync.isPending,
    run: () =>
      sync.mutate(undefined, {
        onSuccess: (r) => {
          const changed = r.created + r.updated + r.removed;
          toast.success(changed > 0 ? `Kalendern är synkad – ${plural(changed, "ändrad händelse", "ändrade händelser")}.` : "Kalendern är synkad – inget nytt.", {
            description: r.trips_with_match ? `${plural(r.trips_with_match, "resa har", "resor har")} en kalenderträff.` : undefined,
          });
        },
        onError: notifyError,
      }),
  };
}

export function IcsLinkSection() {
  const status = useIcsStatus();
  const save = useSaveIcsLink();
  const remove = useRemoveIcsLink();
  const sync = useIcsSyncAction();
  const [link, setLink] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const title = "Kalenderlänk (ICS)";
  const description = "Enklast om du inte kan registrera en app: klistra in en kalenderlänk från Outlook. Ingen inloggning behövs.";

  if (status.isLoading) {
    return (
      <Section title={title} description={description}>
        <SkeletonBlock rows={3} />
      </Section>
    );
  }
  if (status.isError || !status.data) {
    return (
      <Section title={title} description={description}>
        <ErrorState compact error={status.error} onRetry={() => void status.refetch()} title="Kalenderlänkens status kunde inte hämtas" />
      </Section>
    );
  }

  const s = status.data;
  const hasLink = s.has_link;
  const busy = save.isPending || remove.isPending;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const value = link.trim();
    if (!value) return;
    setError(null);
    save.mutate(value, {
      onSuccess: (r) => {
        setLink("");
        toast.success("Kalenderlänken är sparad och krypterad.", { description: `${plural(r.stored_events, "händelse", "händelser")} hämtades.` });
      },
      onError: (err) => setError(errorMessage(err)),
    });
  };

  return (
    <Section title={title} description={description}>
      <div data-testid="ics-section" className="space-y-3">
        <LastErrorNote error={s.last_error} at={s.last_error_at} />

        {hasLink ? (
          <div className="text-[13px] text-fg-muted" data-testid="ics-status">
            <p>
              Sparad länk till <span className="font-medium break-all text-fg">{s.host ?? "kalendertjänsten"}</span>.
            </p>
            {s.last_success_at ? (
              <p>
                Senast synkad {formatDateTime(s.last_success_at)} · {plural(s.stored_events, "händelse", "händelser")} sparade.
              </p>
            ) : null}
          </div>
        ) : null}

        {s.tip ? (
          <Note icon={<Info aria-hidden />} role="status">
            <p data-testid="ics-tip">{s.tip}</p>
          </Note>
        ) : null}

        <form onSubmit={submit} className="space-y-3" noValidate>
          <div data-testid="ics-link-input">
            <SecretField
              label="Kalenderlänk"
              saved={hasLink}
              value={link}
              onChange={(v) => {
                setLink(v);
                if (error) setError(null);
              }}
              placeholder="https://outlook.office365.com/owa/calendar/…/calendar.ics"
              hint={hasLink ? "Klistra in en ny länk för att byta. Den gamla länkens händelser ersätts." : "Länken börjar med https:// eller webcal://."}
            />
          </div>
          <FieldError>{error}</FieldError>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" variant="primary" loading={save.isPending} disabled={!link.trim() || remove.isPending} data-testid="ics-save">
              {hasLink ? "Byt länk" : "Spara"}
            </Button>
            {hasLink ? (
              <Button type="button" onClick={sync.run} loading={sync.isPending} disabled={busy} data-testid="ics-sync">
                <RefreshCw aria-hidden /> Synka nu
              </Button>
            ) : null}
          </div>
        </form>

        <Note icon={<Info aria-hidden />}>
          <p>
            Skapa länken i Outlook på webben under Inställningar → Kalender → Delade kalendrar. Publicera kalendern, välj <span className="font-medium text-fg">Kan visa rubriker och platser</span> och kopiera ICS-länken.
          </p>
          <p>Länken fungerar som ett lösenord – den som har den kan läsa kalendern. DriveLog sparar den krypterad och visar den aldrig igen.</p>
        </Note>

        {hasLink ? (
          <ConfirmButton
            icon={<Trash2 aria-hidden />}
            label="Ta bort länken"
            confirmLabel="Ja, ta bort"
            description="Länken och alla händelser som har hämtats från den raderas, och kalenderträffarna på resorna försvinner. Resorna finns kvar."
            loading={remove.isPending}
            onConfirm={() =>
              remove.mutate(undefined, {
                onSuccess: () => {
                  setLink("");
                  setError(null);
                  toast.success("Kalenderlänken och dess händelser är borttagna.");
                },
                onError: notifyError,
              })
            }
          />
        ) : null}
      </div>
    </Section>
  );
}
