"use client";

import * as React from "react";
import { toast } from "sonner";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { plural } from "@/lib/format";
import type { ImportBatch, ImportCommitResult } from "@/lib/imports-types";
import { useCommitImport, useDiscardPreview, useImportBatch, usePreviewUpload } from "@/lib/queries/imports";
import { readStorage, writeStorage } from "@/lib/utils";
import { useVehicle } from "@/lib/vehicle";
import { ImportHistory } from "./import-history";
import { PreviewStep } from "./preview-step";
import { ReceiptStep } from "./receipt-step";
import { UploadStep } from "./upload-step";

/** Pågående förhandsgranskning, så att en omladdning av sidan inte tappar den. */
const STORAGE_KEY = "drivelog.import.preview";

type Step = { kind: "upload" } | { kind: "preview"; batchId: string } | { kind: "receipt"; result: ImportCommitResult };

/**
 * Import i tre steg på en sida: ladda upp → förhandsgranska → kvitto. Bara det
 * aktuella steget visas; "Tidigare importer" ligger alltid under flödet.
 */
export function ImportView() {
  const { vehicle, vehicles, vehicleId } = useVehicle();
  const [step, setStepState] = React.useState<Step>(() => {
    const stored = typeof window === "undefined" ? null : readStorage<string | null>("session", STORAGE_KEY, null);
    return stored ? { kind: "preview", batchId: stored } : { kind: "upload" };
  });
  const headingRef = React.useRef<HTMLHeadingElement>(null);
  const focusNext = React.useRef(false);

  const setStep = React.useCallback((next: Step, focus = true) => {
    writeStorage("session", STORAGE_KEY, next.kind === "preview" ? next.batchId : null);
    focusNext.current = focus;
    setStepState(next);
  }, []);

  React.useEffect(() => {
    if (!focusNext.current) return;
    focusNext.current = false;
    headingRef.current?.focus({ preventScroll: true });
    headingRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [step]);

  // ------------------------------------------------------------------ uppladdning
  const upload = usePreviewUpload();
  const { mutate: uploadMutate } = upload;
  const [files, setFiles] = React.useState<File[]>([]);
  const [progress, setProgress] = React.useState(0);

  const send = React.useCallback(
    (picked: File[]) => {
      setFiles(picked);
      setProgress(0);
      uploadMutate(
        { files: picked, vehicleId, onProgress: setProgress },
        { onSuccess: (batch) => setStep({ kind: "preview", batchId: batch.id }) },
      );
    },
    [uploadMutate, vehicleId, setStep],
  );

  // ------------------------------------------------------------------ förhandsgranskning
  const batchQuery = useImportBatch(step.kind === "preview" ? step.batchId : undefined);
  const batch = batchQuery.data;

  // En sparad förhandsgranskning som inte längre går att bekräfta (eller hör till ett
  // annat fordon) släpps tyst och uppladdningen visas i stället.
  const dropped =
    step.kind === "preview" &&
    (batchQuery.isError || (!!batch && (batch.status !== "preview" || (vehicleId !== undefined && batch.vehicle_id !== vehicleId))));
  React.useEffect(() => {
    if (dropped) writeStorage("session", STORAGE_KEY, null);
  }, [dropped]);
  const view: Step = dropped ? { kind: "upload" } : step;
  const previewId = view.kind === "preview" ? view.batchId : undefined;

  const commit = useCommitImport();
  const discard = useDiscardPreview();

  const confirm = (applyMergeRules: boolean | undefined) => {
    if (!previewId) return;
    commit.mutate(
      { batchId: previewId, applyMergeRules },
      {
        onSuccess: (result) => {
          const n = result.imported ?? 0;
          toast.success("Importen är klar.", {
            description: n > 0 ? `${plural(n, "resa", "resor")} har lagts till i körjournalen.` : "Inga nya resor tillkom.",
          });
          setStep({ kind: "receipt", result });
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );
  };

  const cancel = () => {
    const id = previewId;
    upload.reset();
    setStep({ kind: "upload" });
    // Inget har sparats – förhandsgranskningen och de uppladdade filerna slängs i bakgrunden.
    if (id && batch?.status === "preview") discard.mutate(id, { onError: (e) => toast.error(errorMessage(e)) });
  };

  const resume = (b: ImportBatch) => setStep({ kind: "preview", batchId: b.id });

  const vehicleNote =
    vehicles.length > 1 && vehicle ? (
      <>
        Resorna hamnar på <span className="font-medium text-fg">{vehicle.name}</span>
        {vehicle.is_demo ? " (demofordon)" : ""}.
      </>
    ) : vehicle?.is_demo ? (
      "Demofordon – importerade resor märks som demo."
    ) : null;

  let current: React.ReactNode;
  if (view.kind === "receipt") {
    current = <ReceiptStep result={view.result} onAgain={() => setStep({ kind: "upload" })} headingRef={headingRef} />;
  } else if (view.kind === "preview" && batch) {
    current = (
      <PreviewStep
        key={batch.id}
        batch={batch}
        onConfirm={confirm}
        onCancel={cancel}
        confirming={commit.isPending}
        note={vehicleNote}
        headingRef={headingRef}
      />
    );
  } else if (view.kind === "preview") {
    current = (
      <Card className="space-y-4 p-6" role="status" aria-label="Laddar förhandsgranskningen">
        <Skeleton className="h-6 w-56" />
        <Skeleton className="h-4 w-80 max-w-full" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-12 w-full" />
      </Card>
    );
  } else {
    current = (
      <UploadStep
        onFiles={send}
        uploading={upload.isPending}
        progress={progress}
        files={files}
        error={upload.error}
        onRetry={() => send(files)}
        note={vehicleNote}
        headingRef={headingRef}
      />
    );
  }

  return (
    <div className="max-w-5xl">
      <PageHeader title="Import" description="Läs in körjournalen från Volvo Cars-appen. Du ser vad som läggs till innan något sparas." />
      <div className="space-y-10">
        {current}
        <ImportHistory
          currentBatchId={previewId}
          onResume={resume}
          onUndone={(id) => {
            if (view.kind === "receipt" && view.result.batch_id === id) setStep({ kind: "upload" }, false);
          }}
        />
      </div>
    </div>
  );
}
