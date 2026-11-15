"use client";

import * as React from "react";
import { DatabaseBackup, Download, Lock, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { SkeletonBlock } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, formatRelative, plural } from "@/lib/format";
import { useMe } from "@/lib/queries/auth";
import { downloadBackup, useBackups, useCreateBackup, useDeleteBackup } from "@/lib/queries/settings-page";
import type { BackupFile } from "@/lib/settings-page-types";
import { ConfirmInline } from "./actions";
import { SectionIntro } from "./form-parts";

function BackupRow({ file }: { file: BackupFile }) {
  const remove = useDeleteBackup();
  const [downloading, setDownloading] = React.useState(false);

  const download = async () => {
    setDownloading(true);
    try {
      await downloadBackup(file.name);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 px-5 py-3" data-testid="backup-row">
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-[13px] text-fg" title={file.name}>
          {file.name}
        </p>
        <p className="mt-0.5 text-xs text-fg-muted">
          <time dateTime={file.created_at} title={formatDateTime(file.created_at)}>
            {formatRelative(file.created_at)}
          </time>{" "}
          · {formatBytes(file.size_bytes)}
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <Button variant="secondary" size="xs" onClick={() => void download()} loading={downloading} aria-label={`Hämta ${file.name}`}>
          {downloading ? null : <Download aria-hidden />} Hämta
        </Button>
        <ConfirmInline
          size="xs"
          label="Radera"
          icon={<Trash2 aria-hidden />}
          ariaLabel={`Radera ${file.name}`}
          confirmLabel="Ja, radera"
          description="Säkerhetskopian tas bort från servern permanent. Har du hämtat den finns din egen kopia kvar."
          loading={remove.isPending}
          onConfirm={() =>
            remove.mutate(file.name, {
              onSuccess: () => toast.success("Säkerhetskopian är raderad."),
              onError: (err) => toast.error(errorMessage(err)),
            })
          }
        />
      </div>
    </li>
  );
}

function Backups() {
  const backups = useBackups();
  const create = useCreateBackup();
  const rows = backups.data ?? [];

  const onCreate = () =>
    create.mutate(undefined, {
      onSuccess: (res) => toast.success(`Säkerhetskopian är skapad (${formatBytes(res.size_bytes)}).`),
      onError: (err) => toast.error(errorMessage(err)),
    });

  return (
    <Card>
      <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-fg">Säkerhetskopior på servern</h3>
          <p className="mt-0.5 text-xs leading-relaxed text-fg-muted">
            {backups.data
              ? rows.length
                ? `${plural(rows.length, "kopia", "kopior")}. Hämta den senaste och spara den på ett annat ställe.`
                : "Inga kopior ännu."
              : "Hämtar…"}
          </p>
        </div>
        <Button variant="primary" onClick={onCreate} loading={create.isPending} data-testid="create-backup">
          {create.isPending ? null : <Plus aria-hidden />} Skapa säkerhetskopia
        </Button>
      </div>
      <div className="border-t border-border">
        {backups.isError ? (
          <ErrorState compact error={backups.error} onRetry={() => backups.refetch()} title="Säkerhetskopiorna gick inte att hämta" />
        ) : !backups.data ? (
          <div className="px-5 py-4">
            <SkeletonBlock rows={2} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            compact
            icon={<DatabaseBackup />}
            title="Ingen säkerhetskopia ännu"
            description="Skapa en kopia och hämta hem den, till exempel innan du uppdaterar servern eller raderar historik."
          />
        ) : (
          <ul className="divide-y divide-border" aria-label="Säkerhetskopior">
            {rows.map((f) => (
              <BackupRow key={f.name} file={f} />
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

/** 11. Säkerhetskopia – endast administratören. */
export function BackupSection() {
  const me = useMe();
  const isAdmin = me.data?.user.is_admin === true;

  return (
    <section aria-labelledby="settings-sakerhetskopia">
      <SectionIntro id="settings-sakerhetskopia" title="Säkerhetskopia">
        En kopia av hela databasen – resor, platser, inställningar och ändringshistorik – i en zip-fil. Sparade nycklar och tokens ligger kvar
        krypterade och kan bara användas med serverns egen krypteringsnyckel.
      </SectionIntro>
      {me.isLoading ? (
        <Card className="p-5">
          <SkeletonBlock rows={3} />
        </Card>
      ) : isAdmin ? (
        <Backups />
      ) : (
        <Card>
          <EmptyState
            compact
            icon={<Lock />}
            title="Endast för administratören"
            description="Säkerhetskopior hanteras av den som administrerar servern. Be hen skapa en kopia om du behöver."
          />
        </Card>
      )}
    </section>
  );
}
