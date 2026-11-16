"use client";

import * as React from "react";
import { BadgeCheck, CalendarDays, CalendarX2, Check, Pencil, RefreshCw, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { CategoryPill } from "@/components/trips/category-pill";
import { CategoryToggle } from "@/components/trips/category-toggle";
import { AiReviewDialog } from "@/components/trips/ai-review-dialog";
import { approvalReasonText, ReviewStatusBadge } from "@/components/trips/review-status-badge";
import { useTripActions } from "@/components/trips/use-trip-actions";
import { errorMessage } from "@/lib/api";
import { formatPercent } from "@/lib/format";
import { DECISION_SOURCE_LABELS, isAutoApproval, label, SUBCATEGORY_LABELS } from "@/lib/labels";
import { useSettings } from "@/lib/queries/settings";
import { useRejectCalendarPurpose, useUpdateTrip } from "@/lib/queries/trips";
import type { PurposeHistoryEntry, TripDetail } from "@/lib/types";

/** Märke framför ärendet: varifrån det kommer och om det väntar på dig. */
function PurposeBadge({ tone, icon, children }: { tone: "accent" | "ok"; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span
      className={
        tone === "ok"
          ? "mr-1.5 inline-flex items-center gap-1 rounded-sm bg-ok-soft px-1.5 py-0.5 align-middle text-[11px] font-semibold text-ok-ink not-italic [&_svg]:size-3"
          : "mr-1.5 inline-flex items-center gap-1 rounded-sm bg-accent-soft px-1.5 py-0.5 align-middle text-[11px] font-semibold text-accent-ink not-italic [&_svg]:size-3"
      }
      data-testid="purpose-badge"
    >
      {icon}
      {children}
    </span>
  );
}

/**
 * "Ärendet stämmer inte" (beslut A): ett ärende som sattes ur kalendern utan fråga tas bort.
 * Resan får i stället ett ärende att bekräfta, och bokningen kopplas inte till resan igen.
 */
export function RejectCalendarPurposeButton({ tripId, size = "sm" }: { tripId: string; size?: "xs" | "sm" }) {
  const reject = useRejectCalendarPurpose();
  return (
    <Button
      size={size}
      variant="secondary"
      loading={reject.isPending}
      data-testid="reject-calendar-purpose"
      onClick={() =>
        reject.mutate(tripId, {
          onSuccess: () => toast.success("Kalenderns ärende är borttaget. Resan väntar på ditt ärende i Granskning."),
          onError: (err) => toast.error(errorMessage(err)),
        })
      }
    >
      {reject.isPending ? null : <CalendarX2 aria-hidden />} Ärendet stämmer inte
    </Button>
  );
}

function currentPurpose(detail: TripDetail): PurposeHistoryEntry | undefined {
  return detail.purpose_history.find((p) => p.is_current);
}

/** Klassning (ett klick), syfte med förslag och godkännande. Överst på resans sida. */
export function ClassificationCard({ detail }: { detail: TripDetail }) {
  const t = detail.trip;
  const actions = useTripActions();
  const update = useUpdateTrip();
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(t.purpose ?? "");

  const suggestions = [
    ...(detail.assessment?.purpose ? [detail.assessment.purpose] : []),
    ...detail.purpose_suggestions.map((s) => s.text),
  ].filter((s, i, arr) => s && arr.indexOf(s) === i && s !== t.purpose);

  const savePurpose = (text: string, approve = false) => {
    update.mutate(
      { id: t.id, patch: { purpose: text, approve } },
      {
        onSuccess: () => {
          toast.success(approve ? "Syftet är godkänt." : "Syftet är sparat.");
          setEditing(false);
        },
        onError: (err) => toast.error(errorMessage(err)),
      },
    );
  };

  const automatic = t.review_status === "approved" && isAutoApproval(t.approval_source);
  const assumed = automatic && t.approval_source === "assumed";
  const startEditing = () => {
    setDraft(t.purpose ?? "");
    setEditing(true);
  };

  const pending = t.review_status === "needs_review" || t.review_status === "proposed" || t.review_status === "auto";
  const proposalPurpose = !t.purpose_confirmed && t.purpose;
  const entry = currentPurpose(detail);
  const fromCalendar = !!t.purpose && t.purpose_confirmed && !!entry?.from_calendar;
  const calendarProposal = !!proposalPurpose && !!entry?.calendar;
  const aiProposal = !!proposalPurpose && !calendarProposal && t.purpose_source === "ai";
  const unsupported = aiProposal && entry?.supported === false;

  // AI-genomgång för just den här resan. Godkända och importerade resor bedöms aldrig om.
  const settings = useSettings();
  const provider = String(settings.data?.settings.ai?.provider ?? "none");
  const [reviewing, setReviewing] = React.useState(false);
  const canReview = t.review_status !== "approved" && t.review_status !== "imported";

  return (
    <Card>
      <CardHeader className="flex-wrap gap-2 pb-2">
        <CardTitle>Klassning</CardTitle>
        <ReviewStatusBadge status={t.review_status} approvalSource={t.approval_source} approvalReason={t.approval_reason} />
      </CardHeader>
      <CardContent className="space-y-5">
        {automatic ? (
          <div
            className={assumed ? "flex items-start justify-between gap-3 rounded-lg border border-warn/40 bg-warn-soft/60 p-3" : "flex items-start justify-between gap-3 rounded-lg border border-border bg-surface-2/50 p-3"}
            data-testid="auto-approval"
          >
            <p className="text-[13px] leading-relaxed text-fg">
              {assumed ? (
                <>
                  <span className="font-semibold">Antaget</span> · {approvalReasonText(t.approval_source, t.approval_reason)}. Det är DriveLogs antagande, inte ditt
                  beslut. Stämmer det inte, byt kategori eller ärende.
                </>
              ) : (
                <>
                  <span className="font-semibold">Godkänd automatiskt</span> · {approvalReasonText(t.approval_source, t.approval_reason)}. Ändrar du något gäller
                  ditt beslut.
                </>
              )}
            </p>
            <Button size="xs" variant="secondary" className="shrink-0" onClick={startEditing}>
              <Pencil aria-hidden /> Ändra
            </Button>
          </div>
        ) : null}
        <div>
          {t.has_mixed_categories ? (
            <div className="space-y-2">
              <CategoryPill category={t.category} mixed />
              <p className="text-xs text-fg-muted">Resan är fördelad på flera kategorier – se delsträckor nedan.</p>
            </div>
          ) : (
            <CategoryToggle value={t.category} size="lg" onChange={(c) => void actions.setCategory(t.id, c).catch(() => {})} />
          )}
          <p className="mt-2 text-xs text-fg-muted">
            {t.category_source === "user" ? "Avgjord av dig" : `Avgjord av ${label(DECISION_SOURCE_LABELS, t.category_source).toLowerCase()}`}
            {t.confidence != null && (t.category_source === "ai" || t.category_source === "rule" || t.category_source === "pattern")
              ? ` · ${formatPercent(t.confidence)} säkerhet`
              : ""}
            {t.subcategory ? ` · ${label(SUBCATEGORY_LABELS, t.subcategory)}` : ""}
          </p>
          {pending || canReview ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {pending ? (
                <Button variant="primary" size="sm" onClick={() => void actions.approve(t.id).catch(() => {})} loading={actions.isPending}>
                  <BadgeCheck aria-hidden /> Godkänn klassningen
                </Button>
              ) : null}
              {canReview ? (
                <Button variant="secondary" size="sm" onClick={() => setReviewing(true)} data-testid="trip-ai-review">
                  {provider !== "none" ? <Sparkles aria-hidden /> : <RefreshCw aria-hidden />}
                  {provider !== "none" ? "Bedöm med AI" : "Bedöm igen"}
                </Button>
              ) : null}
            </div>
          ) : null}
          {canReview ? <AiReviewDialog open={reviewing} onOpenChange={setReviewing} selection={{ kind: "trips", ids: [t.id] }} trips={[t]} /> : null}
        </div>

        <div className="border-t border-border pt-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-[13px] font-medium text-fg-muted">Syfte</h3>
            {!editing ? (
              <Button size="xs" variant="ghost" onClick={startEditing}>
                <Pencil aria-hidden /> {t.purpose ? "Ändra" : "Lägg till"}
              </Button>
            ) : null}
          </div>
          {editing ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (draft.trim()) savePurpose(draft.trim());
              }}
              className="flex gap-2"
            >
              <Input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} maxLength={500} aria-label="Syfte" />
              <Button type="submit" variant="primary" loading={update.isPending} disabled={!draft.trim()}>
                Spara
              </Button>
              <Button variant="ghost" onClick={() => setEditing(false)}>
                Avbryt
              </Button>
            </form>
          ) : t.purpose ? (
            <div>
              <p className={proposalPurpose ? "text-[15px] text-fg-muted italic" : "text-[15px] font-medium"}>
                {calendarProposal ? (
                  <PurposeBadge tone="accent" icon={<CalendarDays aria-hidden />}>
                    Förslag ur kalendern
                  </PurposeBadge>
                ) : aiProposal ? (
                  <PurposeBadge tone="accent" icon={<Sparkles aria-hidden />}>
                    Förslag att bekräfta
                  </PurposeBadge>
                ) : proposalPurpose ? (
                  <PurposeBadge tone="accent" icon={<Sparkles aria-hidden />}>
                    Förslag
                  </PurposeBadge>
                ) : fromCalendar ? (
                  <PurposeBadge tone="ok" icon={<CalendarDays aria-hidden />}>
                    Ur kalendern
                  </PurposeBadge>
                ) : null}
                {t.purpose}
              </p>
              {unsupported ? (
                <p className="mt-1 text-xs text-warn-ink" data-testid="purpose-unsupported">
                  AI:s förslag utan direkt stöd i underlaget – kontrollera innan du godkänner.
                </p>
              ) : null}
              {fromCalendar ? (
                <p className="mt-1 text-xs text-fg-muted">
                  Ärendet kommer ur en kalenderbokning som stämde med resan och sattes när resan godkändes automatiskt.
                </p>
              ) : null}
              {proposalPurpose || fromCalendar ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {proposalPurpose ? (
                    <Button size="sm" variant="secondary" onClick={() => savePurpose(t.purpose as string, true)} loading={update.isPending}>
                      <Check aria-hidden /> Godkänn syftet
                    </Button>
                  ) : null}
                  {fromCalendar ? <RejectCalendarPurposeButton tripId={t.id} /> : null}
                </div>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-fg-subtle">Inget syfte angivet.</p>
          )}
          {suggestions.length ? (
            <div className="mt-3">
              <p className="mb-1.5 text-xs text-fg-muted">Tidigare syften</p>
              <div className="flex flex-wrap gap-1.5">
                {suggestions.slice(0, 6).map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => savePurpose(s)}
                    className="rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-fg transition-colors hover:border-accent hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-ring"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
