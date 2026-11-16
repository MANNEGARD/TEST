"use client";

import * as React from "react";
import { BellOff, ExternalLink, Home, Send } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { SkeletonBlock } from "@/components/ui/skeleton";
import type { DemoPhoneNotification, NotificationAction } from "@/lib/integrations-types";
import { useDemoInbox, useDemoRespond } from "@/lib/queries/integrations";
import { DemoTag, notifyError, TimeAgo } from "./shared";

/** Sökvägen som ett tryck på själva notisen öppnar (data.url i tjänstedatan), utan värdnamn. */
function notificationTapPath(n: DemoPhoneNotification): string | null {
  const data = n.ha_payload?.data;
  const url = data && typeof data === "object" ? (data as Record<string, unknown>).url : undefined;
  if (typeof url !== "string" || !url) return null;
  try {
    const parsed = new URL(url, "http://localhost");
    return parsed.pathname.startsWith("/") ? parsed.pathname + parsed.search : null;
  } catch {
    return null;
  }
}

function PhoneNotification({
  notification: n,
  onSend,
  pendingAction,
  busy,
}: {
  notification: DemoPhoneNotification;
  onSend: (action: NotificationAction, replyText?: string) => void;
  /** Åtgärden som just nu skickas för den här notisen. */
  pendingAction: string | null;
  busy: boolean;
}) {
  const [replyFor, setReplyFor] = React.useState<NotificationAction | null>(null);
  const [reply, setReply] = React.useState("");
  const tapPath = notificationTapPath(n);

  return (
    <li className="rounded-xl border border-border bg-surface p-3 shadow-sm" data-testid="demo-notification" data-kind={n.kind}>
      <div className="flex items-center gap-1.5 text-[11px] font-medium tracking-wide text-fg-muted uppercase">
        <Home className="size-3" aria-hidden /> Home Assistant
        <span className="ml-auto font-normal tracking-normal normal-case">
          <TimeAgo value={n.sent_at} empty="" />
        </span>
      </div>
      <h4 className="mt-1.5 text-sm font-semibold text-fg">
        {tapPath ? (
          <a href={tapPath} target="_blank" rel="noreferrer" className="rounded-sm hover:underline focus-visible:outline-2 focus-visible:outline-ring">
            {n.title}
          </a>
        ) : (
          n.title
        )}
      </h4>
      <p className="mt-0.5 text-[13px] leading-snug text-fg">{n.message}</p>

      <div className="mt-2.5 grid grid-cols-2 gap-1.5">
        {n.actions.map((a) =>
          a.uri ? (
            <Button key={a.key} asChild size="sm" variant="subtle">
              <a href={a.uri} target="_blank" rel="noreferrer">
                {a.title} <ExternalLink aria-hidden />
              </a>
            </Button>
          ) : a.text_input ? (
            <Button key={a.key} size="sm" variant="subtle" aria-expanded={replyFor?.key === a.key} onClick={() => setReplyFor(replyFor?.key === a.key ? null : a)}>
              {a.title}
            </Button>
          ) : (
            <Button key={a.key} size="sm" variant="subtle" loading={pendingAction === a.key} disabled={busy} onClick={() => onSend(a)}>
              {a.title}
            </Button>
          ),
        )}
      </div>

      {replyFor ? (
        <form
          className="mt-2 flex gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (reply.trim()) onSend(replyFor, reply.trim());
          }}
        >
          <Input
            autoFocus
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            placeholder="t.ex. Kundmöte hos Exempel AB"
            aria-label={replyFor.title}
            maxLength={200}
            className="h-8 text-[13px]"
          />
          <Button type="submit" size="icon-sm" variant="primary" aria-label="Skicka svar" loading={pendingAction === replyFor.key} disabled={busy || !reply.trim()}>
            {pendingAction === replyFor.key ? null : <Send aria-hidden />}
          </Button>
        </form>
      ) : null}

      <details className="mt-2 text-xs">
        <summary className="cursor-pointer rounded-sm text-fg-subtle select-none hover:text-fg focus-visible:outline-2 focus-visible:outline-ring">Tjänstedata till Home Assistant</summary>
        <pre className="scrollbar-thin mt-1.5 max-h-48 overflow-auto rounded-md bg-surface-2 p-2 font-mono text-[11px] leading-relaxed text-fg-muted">{JSON.stringify(n.ha_payload, null, 2)}</pre>
      </details>
    </li>
  );
}

/**
 * Simulerad telefon för Home Assistants demoläge: visar levererade notiser med
 * fungerande knappar, så att hela flödet kan provas utan iPhone.
 */
export function DemoPhone() {
  const inbox = useDemoInbox(true);
  // Mutationen ägs av telefonen, inte av notisen: en besvarad notis försvinner ur
  // listan innan kvittot hinner visas om den själv äger anropet.
  const respond = useDemoRespond();
  const list = inbox.data ?? [];
  const pending = respond.isPending ? respond.variables : undefined;

  const send = (publicId: string, action: NotificationAction, replyText?: string) =>
    respond.mutate(
      { publicId, action: action.key, replyText },
      {
        onSuccess: (r) => {
          const ok = r.outcome === "applied" || r.outcome === "snoozed" || r.outcome === "opened";
          (ok ? toast.success : toast.warning)(r.message, { description: r.follow_up ? "En följdfråga har skickats till telefonen." : undefined });
        },
        onError: notifyError,
      },
    );
  return (
    <div className="mx-auto w-full max-w-[340px] rounded-[2rem] border-[5px] border-border-strong bg-surface-2 p-3 shadow-md" data-testid="demo-phone">
      <div className="mx-auto mb-3 h-1.5 w-14 rounded-full bg-border-strong" aria-hidden />
      <div className="mb-3 flex items-center justify-between gap-2 px-1">
        <h4 className="text-[13px] font-semibold text-fg">Simulerad telefon</h4>
        <DemoTag />
      </div>
      {inbox.isLoading ? (
        <SkeletonBlock rows={4} className="rounded-xl bg-surface p-3" />
      ) : inbox.isError ? (
        <ErrorState compact error={inbox.error} onRetry={() => void inbox.refetch()} title="Notiserna kunde inte hämtas" />
      ) : list.length === 0 ? (
        <div className="flex flex-col items-center gap-1.5 px-3 py-8 text-center">
          <BellOff className="size-5 text-fg-subtle" aria-hidden />
          <p className="text-[13px] font-medium text-fg">Inga notiser just nu</p>
          <p className="text-xs leading-relaxed text-fg-muted">Skicka en testnotis eller simulera en resa – frågan dyker upp här inom några sekunder.</p>
        </div>
      ) : (
        <ul className="space-y-2.5" aria-label="Notiser på den simulerade telefonen" aria-live="polite">
          {list.map((n) => (
            <PhoneNotification
              key={n.public_id}
              notification={n}
              busy={respond.isPending}
              pendingAction={pending?.publicId === n.public_id ? pending.action : null}
              onSend={(action, replyText) => send(n.public_id, action, replyText)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
