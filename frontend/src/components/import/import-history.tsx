"use client";

import * as React from "react";
import { Download, FileX2, History, MoreHorizontal, Play, Trash2, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, errorMessage } from "@/lib/api";
import { formatDate, formatNumber, plural } from "@/lib/format";
import type { ImportBatch } from "@/lib/imports-types";
import { BATCH_STATUS_LABELS, label } from "@/lib/labels";
import { downloadImportFile, useDeleteImportFiles, useDiscardPreview, useImportBatches, useUndoImport } from "@/lib/queries/imports";
import { cn } from "@/lib/utils";
import { periodLabel } from "./import-labels";

type Tone = "neutral" | "ok" | "err" | "accent";

function statusBadge(b: ImportBatch): { label: string; tone: Tone } {
  if (b.status === "preview") return { label: "Ej bekräftad", tone: "accent" };
  if (b.status === "committed") return { label: "Importerad", tone: "ok" };
  if (b.status === "failed") return { label: label(BATCH_STATUS_LABELS, b.status), tone: "err" };
  return { label: label(BATCH_STATUS_LABELS, b.status), tone: "neutral" };
}

/** Antal resor importen gav (eller skulle ge, för en förhandsgranskning). */
function tripCount(b: ImportBatch): number {
  return b.summary.imported ?? b.summary.rows_new ?? 0;
}

type Pending = { kind: "undo" | "files"; batch: ImportBatch } | null;

function ConfirmDialog({ pending: open, onClose, onUndone }: { pending: Pending; onClose: () => void; onUndone?: (batchId: string) => void }) {
  // Behåll innehållet medan dialogen animeras bort.
  const [pending, setPending] = React.useState<Pending>(open);
  if (open && open !== pending) setPending(open);
  const undo = useUndoImport();
  const deleteFiles = useDeleteImportFiles();
  const busy = undo.isPending || deleteFiles.isPending;
  const b = pending?.batch;
  const n = b ? tripCount(b) : 0;

  const run = () => {
    if (!pending) return;
    if (pending.kind === "undo") {
      undo.mutate(pending.batch.id, {
        onSuccess: (r) => {
          toast.success("Importen är ångrad.", {
            description: r.voided ? `${plural(r.voided, "resa", "resor")} togs bort ur körjournalen.` : undefined,
          });
          onUndone?.(pending.batch.id);
          onClose();
        },
        onError: (e) => toast.error(errorMessage(e)),
      });
    } else {
      deleteFiles.mutate(pending.batch.id, {
        onSuccess: (r) => {
          toast.success(r.removed ? "Originalfilerna är raderade." : "Det fanns inga sparade originalfiler.", {
            description: r.removed ? "Resorna i körjournalen påverkas inte." : undefined,
          });
          onClose();
        },
        onError: (e) => toast.error(errorMessage(e)),
      });
    }
  };

  return (
    <Dialog open={!!open} onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="max-w-md">
        {pending?.kind === "undo" ? (
          <DialogHeader>
            <DialogTitle>Ångra importen?</DialogTitle>
            <DialogDescription>
              {n > 0 ? `${plural(n, "resa", "resor")} från ` : "Resorna från "}
              {b ? formatDate(b.committed_at ?? b.created_at) : ""} tas bort ur körjournalen och sammanslagningar som berör dem löses upp. Du kan importera
              filen igen senare.
            </DialogDescription>
          </DialogHeader>
        ) : (
          <DialogHeader>
            <DialogTitle>Radera originalfilerna?</DialogTitle>
            <DialogDescription>
              De uppladdade filerna tas bort från servern och kan inte hämtas igen. Resorna i körjournalen påverkas inte.
            </DialogDescription>
          </DialogHeader>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Avbryt
          </Button>
          <Button variant="danger" onClick={run} loading={busy} data-testid="import-confirm-action">
            {pending?.kind === "undo" ? "Ångra import" : "Radera filer"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BatchRow({
  batch,
  onUndo,
  onDeleteFiles,
  onResume,
  onDiscard,
  discarding,
}: {
  batch: ImportBatch;
  onUndo: () => void;
  onDeleteFiles: () => void;
  onResume: () => void;
  onDiscard: () => void;
  discarding: boolean;
}) {
  const files = batch.summary.files ?? [];
  const status = statusBadge(batch);
  const n = tripCount(batch);
  const isPreview = batch.status === "preview";
  const downloadable = files.filter((f) => f.status !== "error");
  const fileNames = files.map((f) => f.filename).join(", ");

  return (
    <li className="flex items-center gap-3 px-4 py-3 sm:px-5" data-testid="import-history-row">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-medium num">{formatDate(batch.committed_at ?? batch.created_at)}</span>
          <Badge tone={status.tone} size="sm">
            {status.label}
          </Badge>
        </div>
        <p className="mt-0.5 truncate text-[13px] text-fg-muted" title={fileNames}>
          {files.length > 1 ? `${plural(files.length, "fil", "filer")}: ` : ""}
          {fileNames || "Inga filer"}
        </p>
      </div>
      <div className="shrink-0 text-right">
        <p className={cn("text-sm whitespace-nowrap", batch.status === "undone" && "text-fg-muted line-through decoration-fg-subtle")}>
          <span className="font-mono font-medium">{formatNumber(n, 0)}</span> {n === 1 ? "resa" : "resor"}
        </p>
        {batch.summary.period_start ? (
          <p className="hidden text-xs text-fg-subtle num sm:block">{periodLabel(batch.summary.period_start, batch.summary.period_end)}</p>
        ) : null}
      </div>
      {isPreview ? (
        <Button size="sm" variant="secondary" onClick={onResume} className="shrink-0">
          <Play aria-hidden /> Fortsätt
        </Button>
      ) : null}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="icon-sm" variant="ghost" aria-label={`Åtgärder för importen ${formatDate(batch.committed_at ?? batch.created_at)}`} className="shrink-0" loading={discarding}>
            {discarding ? null : <MoreHorizontal aria-hidden />}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="max-w-[min(20rem,calc(100vw-2rem))]">
          {downloadable.length ? (
            <>
              <DropdownMenuLabel>{downloadable.length === 1 ? "Hämta originalfil" : "Hämta originalfiler"}</DropdownMenuLabel>
              {downloadable.map((f) => (
                <DropdownMenuItem
                  key={f.id}
                  onSelect={() => {
                    downloadImportFile(batch.id, f.id, f.filename).catch((e) =>
                      toast.error(e instanceof ApiError && e.status === 404 ? "Originalfilen finns inte längre – den har raderats." : errorMessage(e)),
                    );
                  }}
                >
                  <Download aria-hidden /> <span className="truncate">{f.filename}</span>
                </DropdownMenuItem>
              ))}
              <DropdownMenuSeparator />
            </>
          ) : null}
          {isPreview ? (
            <DropdownMenuItem onSelect={onDiscard} destructive>
              <Trash2 aria-hidden /> Släng förhandsgranskningen
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem onSelect={onDeleteFiles}>
              <FileX2 aria-hidden /> Radera originalfiler
            </DropdownMenuItem>
          )}
          {batch.status === "committed" ? (
            <DropdownMenuItem onSelect={onUndo} destructive data-testid="import-undo">
              <Undo2 aria-hidden /> Ångra import
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

/**
 * "Tidigare importer" för valt fordon. Förhandsgranskningar som slängts visas inte;
 * en som lämnats öppen kan återupptas.
 */
export function ImportHistory({
  currentBatchId,
  onResume,
  onUndone,
}: {
  currentBatchId?: string;
  onResume: (batch: ImportBatch) => void;
  /** Anropas när en import har ångrats (t.ex. för att stänga dess kvitto). */
  onUndone?: (batchId: string) => void;
}) {
  const q = useImportBatches();
  const discard = useDiscardPreview();
  const [pending, setPending] = React.useState<Pending>(null);

  const items = (q.data?.items ?? []).filter((b) => b.id !== currentBatchId && !(b.status === "undone" && !b.committed_at));

  let body: React.ReactNode;
  if (q.isError) body = <ErrorState compact error={q.error} onRetry={() => void q.refetch()} title="Importerna kunde inte hämtas" />;
  else if (!q.data)
    body = (
      <div className="space-y-4 p-5" role="status" aria-label="Laddar">
        {[0, 1].map((i) => (
          <div key={i} className="flex items-center gap-3">
            <div className="flex-1 space-y-2">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-3 w-64 max-w-full" />
            </div>
            <Skeleton className="h-4 w-16" />
          </div>
        ))}
      </div>
    );
  else if (!items.length)
    body = (
      <EmptyState
        compact
        icon={<History />}
        title="Inga importer ännu"
        description="När du har importerat en körjournal visas den här. Du kan ångra en import eller hämta originalfilen i efterhand."
      />
    );
  else
    body = (
      <ul className="divide-y divide-border">
        {items.map((b) => (
          <BatchRow
            key={b.id}
            batch={b}
            onUndo={() => setPending({ kind: "undo", batch: b })}
            onDeleteFiles={() => setPending({ kind: "files", batch: b })}
            onResume={() => onResume(b)}
            discarding={discard.isPending && discard.variables === b.id}
            onDiscard={() =>
              discard.mutate(b.id, {
                onSuccess: () => toast.success("Förhandsgranskningen är borttagen."),
                onError: (e) => toast.error(errorMessage(e)),
              })
            }
          />
        ))}
      </ul>
    );

  return (
    <section aria-labelledby="import-history-title" className="space-y-3">
      <h2 id="import-history-title" className="px-1 text-sm font-semibold tracking-tight">
        Tidigare importer
      </h2>
      <Card data-testid="import-history">{body}</Card>
      <ConfirmDialog pending={pending} onClose={() => setPending(null)} onUndone={onUndone} />
    </section>
  );
}
