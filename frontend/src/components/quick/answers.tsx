"use client";

import * as React from "react";
import { Briefcase, Check, Clock, Home, Merge, Split } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import type { Question } from "@/lib/notifications-types";
import { cn } from "@/lib/utils";

/** Åtgärdsnycklar (backend/app/services/ha/actions.py). */
export const KEY = {
  work: "WORK",
  private: "PRIVATE",
  review: "REVIEW",
  open: "OPEN",
  later: "LATER",
  mergeYes: "MERGEYES",
  mergeNo: "MERGENO",
  purposeText: "PURPOSETEXT",
  purposePick: "PURPOSE",
  testOk: "TESTOK",
  // Självgående läge (1.1.0): kategori och ärende i samma tryck.
  workText: "WORKTEXT",
  privateAlways: "PRIVALWAYS",
  companyText: "COMPANYTEXT",
  ok: "OK",
  openSecond: "OPEN1",
} as const;

/** Navigering (Granska/Öppna) ändrar inget – de blir länkar i stället för svar. */
export const NAVIGATION_KEYS = new Set<string>([KEY.review, KEY.open, KEY.openSecond]);

export const MAX_PURPOSE_LENGTH = 500;
/** Textsvar på frågorna i självgående läge: högst 200 tecken (backend tvättar texten). */
export const MAX_REPLY_LENGTH = 200;

export interface AnswerProps {
  question: Question;
  /** Skickar svaret; `replyText` för fritext, `always` för "Gör alltid så här". */
  onAnswer: (action: string, replyText?: string, always?: boolean) => void;
  /** Åtgärden som skickas just nu (för spinner på rätt knapp). */
  pending: string | null;
  /** Fritexten som skickas just nu. */
  pendingText?: string | null;
}

const bigChoice =
  "h-auto min-h-16 flex-col gap-1 rounded-xl border-2 px-4 py-3 text-base font-semibold shadow-sm whitespace-normal [&_svg]:size-5";

function has(q: Question, key: string) {
  return q.actions.some((a) => a.key === key);
}

/** Arbete / Privat – två stora knappar. Bilens förslag märks men väljs aldrig åt användaren. */
function ClassifyAnswers({ question: q, onAnswer, pending }: AnswerProps) {
  const proposal = q.trip && (q.trip.review_status === "proposed" || q.trip.review_status === "auto") ? q.trip.category : null;
  const busy = pending !== null;
  return (
    <div className="grid grid-cols-2 gap-3">
      {has(q, KEY.work) ? (
        <Button
          className={cn(bigChoice, "border-work/40 bg-work-soft text-work-ink hover:border-work")}
          onClick={() => onAnswer(KEY.work)}
          loading={pending === KEY.work}
          disabled={busy}
        >
          {pending === KEY.work ? null : <Briefcase aria-hidden />}
          Arbete
          <span className={cn("text-xs font-normal opacity-80", proposal !== "work" && "invisible")}>Förslag</span>
        </Button>
      ) : null}
      {has(q, KEY.private) ? (
        <Button
          className={cn(bigChoice, "border-private/40 bg-private-soft text-private-ink hover:border-private")}
          onClick={() => onAnswer(KEY.private)}
          loading={pending === KEY.private}
          disabled={busy}
        >
          {pending === KEY.private ? null : <Home aria-hidden />}
          Privat
          <span className={cn("text-xs font-normal opacity-80", proposal !== "private" && "invisible")}>Förslag</span>
        </Button>
      ) : null}
    </div>
  );
}

/** Fritext för syfte: textfält + Spara. Används både i frågan och i följdfrågan efter "Arbete". */
export function PurposeInput({
  onSave,
  loading,
  disabled,
  initial = "",
  id = "quick-purpose",
  label = "Syfte med resan",
  placeholder = "T.ex. Kundmöte hos Acme",
  maxLength = MAX_PURPOSE_LENGTH,
}: {
  onSave: (text: string) => void;
  loading?: boolean;
  disabled?: boolean;
  initial?: string;
  id?: string;
  label?: string;
  placeholder?: string;
  maxLength?: number;
}) {
  const [text, setText] = React.useState(initial);
  const trimmed = text.trim();
  return (
    <form
      className="flex gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (trimmed) onSave(trimmed);
      }}
    >
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <Input
        id={id}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        maxLength={maxLength}
        autoComplete="off"
        enterKeyHint="done"
        className="h-12 flex-1 text-base"
      />
      <Button type="submit" variant="primary" size="lg" className="h-12" loading={loading} disabled={disabled || !trimmed}>
        {loading ? null : <Check aria-hidden />}
        Spara
      </Button>
    </form>
  );
}

/** Syfte: tidigare syften som knappar + eget syfte i textfält. */
function PurposeAnswers({ question: q, onAnswer, pending, pendingText }: AnswerProps) {
  const picks = q.actions.filter((a) => a.key.startsWith(KEY.purposePick) && a.key !== KEY.purposeText);
  const busy = pending !== null;
  // Ett obekräftat syftesförslag på resan kan godkännas med ett tryck (sparas som fritext).
  const proposed = q.trip && !q.trip.purpose_confirmed ? q.trip.purpose?.trim() : null;
  const showProposed = !!proposed && has(q, KEY.purposeText) && !picks.some((a) => a.title === proposed);
  const proposedPending = pending === KEY.purposeText && pendingText === proposed;
  return (
    <div className="space-y-5">
      {showProposed ? (
        <div>
          <p className="mb-2 text-sm text-fg-muted">Förslag</p>
          <Button
            size="lg"
            className="h-auto min-h-12 w-full justify-start rounded-xl border-accent/40 bg-accent-soft py-3 text-left text-base whitespace-normal text-accent-ink hover:border-accent"
            onClick={() => onAnswer(KEY.purposeText, proposed ?? "")}
            loading={proposedPending}
            disabled={busy}
          >
            {proposed}
          </Button>
        </div>
      ) : null}
      {picks.length ? (
        <div>
          <p className="mb-2 text-sm text-fg-muted">{showProposed ? "Eller ett tidigare syfte" : "Välj ett tidigare syfte"}</p>
          <div className="grid gap-2">
            {picks.map((a) => (
              <Button
                key={a.key}
                size="lg"
                className="h-auto min-h-12 justify-start rounded-xl py-3 text-left text-base whitespace-normal"
                onClick={() => onAnswer(a.key)}
                loading={pending === a.key}
                disabled={busy}
              >
                {a.title}
              </Button>
            ))}
          </div>
        </div>
      ) : null}
      {has(q, KEY.purposeText) ? (
        <div>
          <p className="mb-2 text-sm text-fg-muted">{picks.length || showProposed ? "…eller skriv ett eget" : "Skriv syftet"}</p>
          <PurposeInput onSave={(text) => onAnswer(KEY.purposeText, text)} loading={pending === KEY.purposeText && !proposedPending} disabled={busy} />
        </div>
      ) : null}
      {has(q, KEY.private) ? (
        <Button variant="secondary" size="lg" className="w-full rounded-xl" onClick={() => onAnswer(KEY.private)} loading={pending === KEY.private} disabled={busy}>
          {pending === KEY.private ? null : <Home aria-hidden />}
          Privat
        </Button>
      ) : null}
    </div>
  );
}

/**
 * Frågorna i självgående läge (Ny resa, Vilket företag, Antaget): en stor knapp per val i
 * notisens ordning och textfältet för ärende eller företag. "Gör alltid så här" sparar en
 * egen regel när backend tillåter det.
 */
function ChoiceAnswers({ question: q, onAnswer, pending, pendingText }: AnswerProps) {
  const [always, setAlways] = React.useState(false);
  const busy = pending !== null;
  const buttons = q.actions.filter((a) => !a.text_input && !a.uri && !NAVIGATION_KEYS.has(a.key) && a.key !== KEY.later);
  const text = q.actions.find((a) => a.text_input);
  const offerAlways = !!text && text.key !== KEY.companyText;
  return (
    <div className="space-y-5">
      {buttons.length ? (
        <div className="grid gap-2">
          {buttons.map((a, i) => (
            <Button
              key={a.key}
              size="lg"
              variant={i === 0 ? "primary" : "secondary"}
              className="h-auto min-h-12 justify-start rounded-xl py-3 text-left text-base whitespace-normal"
              onClick={() => onAnswer(a.key)}
              loading={pending === a.key}
              disabled={busy}
            >
              {a.title}
            </Button>
          ))}
        </div>
      ) : null}
      {text ? (
        <div>
          <p className="mb-2 text-sm text-fg-muted">{text.title}</p>
          <PurposeInput
            id={`quick-${text.key.toLowerCase()}`}
            label={text.title}
            placeholder={text.key === KEY.companyText ? "T.ex. Bring" : "T.ex. Kundbesök Acme"}
            maxLength={MAX_REPLY_LENGTH}
            onSave={(value) => onAnswer(text.key, value, offerAlways && always)}
            loading={pending === text.key && pendingText !== undefined}
            disabled={busy}
          />
          {offerAlways ? (
            <label className="mt-3 flex items-center gap-2 text-sm text-fg-muted">
              <Checkbox checked={always} onCheckedChange={(v) => setAlways(v === true)} disabled={busy} />
              Gör alltid så här för den här platsen
            </label>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Slå samman: Ja / Nej. */
function MergeAnswers({ question: q, onAnswer, pending }: AnswerProps) {
  const busy = pending !== null;
  return (
    <div className="grid grid-cols-2 gap-3">
      {has(q, KEY.mergeYes) ? (
        <Button className={cn(bigChoice, "border-accent/40 bg-accent-soft text-accent-ink hover:border-accent")} onClick={() => onAnswer(KEY.mergeYes)} loading={pending === KEY.mergeYes} disabled={busy}>
          {pending === KEY.mergeYes ? null : <Merge aria-hidden />}
          Ja, slå samman
        </Button>
      ) : null}
      {has(q, KEY.mergeNo) ? (
        <Button className={cn(bigChoice, "border-border bg-surface text-fg hover:border-border-strong")} onClick={() => onAnswer(KEY.mergeNo)} loading={pending === KEY.mergeNo} disabled={busy}>
          {pending === KEY.mergeNo ? null : <Split aria-hidden />}
          Nej, två resor
        </Button>
      ) : null}
    </div>
  );
}

/** Övriga svarsknappar (t.ex. testnotisens "Fungerar") – en stor knapp per åtgärd. */
function GenericAnswers({ question: q, onAnswer, pending }: AnswerProps) {
  const handled = new Set<string>([KEY.later, KEY.work, KEY.private, KEY.mergeYes, KEY.mergeNo, KEY.purposeText]);
  const rest = q.actions.filter((a) => !NAVIGATION_KEYS.has(a.key) && !a.uri && !handled.has(a.key) && !a.key.startsWith(KEY.purposePick) && !a.text_input);
  if (!rest.length) return null;
  return (
    <div className="grid gap-3">
      {rest.map((a, i) => (
        <Button key={a.key} variant={i === 0 ? "primary" : "secondary"} size="xl" className="rounded-xl" onClick={() => onAnswer(a.key)} loading={pending === a.key} disabled={pending !== null}>
          {a.title}
        </Button>
      ))}
    </div>
  );
}

/** Svarsknapparna för frågans typ, plus "Senare" när frågan kan skjutas upp. */
export function Answers(props: AnswerProps) {
  const { question: q, onAnswer, pending } = props;
  const selfDriving = q.kind === "company" || q.kind === "assumed" || (q.kind === "classify" && !has(q, KEY.work));
  const body =
    selfDriving ? (
      <ChoiceAnswers {...props} />
    ) : q.kind === "classify" ? (
      <ClassifyAnswers {...props} />
    ) : q.kind === "purpose" ? (
      <PurposeAnswers {...props} />
    ) : q.kind === "merge" ? (
      <MergeAnswers {...props} />
    ) : (
      <GenericAnswers {...props} />
    );
  const canSnooze = has(q, KEY.later) || q.kind === "purpose";
  return (
    <div className="space-y-3">
      {body}
      {canSnooze ? (
        <Button variant="ghost" size="lg" className="w-full rounded-xl" onClick={() => onAnswer(KEY.later)} loading={pending === KEY.later} disabled={pending !== null}>
          {pending === KEY.later ? null : <Clock aria-hidden />}
          Senare
        </Button>
      ) : null}
    </div>
  );
}
