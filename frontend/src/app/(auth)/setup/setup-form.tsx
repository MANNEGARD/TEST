"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, Check, Circle } from "lucide-react";
import { PasswordInput, passwordProblems } from "@/components/auth/password-field";
import { retryUntil, useCountdown } from "@/components/auth/rate-limit";
import { Button } from "@/components/ui/button";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, errorMessage } from "@/lib/api";
import { useSetup, useSetupStatus } from "@/lib/queries/auth";
import { cn } from "@/lib/utils";

export function SetupForm() {
  const router = useRouter();
  const status = useSetupStatus();
  const setup = useSetup();
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [touched, setTouched] = React.useState(false);
  const [retryAfter, setRetryAfter] = React.useState<number | null>(null);
  const wait = useCountdown(retryAfter);

  React.useEffect(() => {
    if (status.data && !status.data.needs_setup && !setup.isSuccess) router.replace("/login");
  }, [status.data, router, setup.isSuccess]);

  const checks = passwordProblems(password);
  const pwOk = checks.every((c) => c.ok);
  const emailOk = /^[^\s@]+@[^\s@]+$/.test(email.trim());
  const match = password === confirm;
  const canSubmit = emailOk && pwOk && match && wait === 0;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!canSubmit) return;
    setup.mutate(
      { email: email.trim(), password, display_name: name.trim() },
      {
        onSuccess: () => router.replace("/"),
        onError: (err) => {
          if (err instanceof ApiError && err.status === 429) setRetryAfter(retryUntil(err.retryAfterSeconds));
          if (err instanceof ApiError && err.code === "already_setup") router.replace("/login");
        },
      },
    );
  };

  if (status.isLoading) {
    return <Skeleton className="h-[480px] w-full rounded-xl" />;
  }

  return (
    <div className="rounded-xl border border-border bg-surface p-6 shadow-md sm:p-8">
      <p className="text-xs font-semibold tracking-wider text-accent-ink uppercase">Första start</p>
      <h1 className="mt-1 text-xl font-semibold tracking-tight">Skapa ditt konto</h1>
      <p className="mt-1 text-sm text-fg-muted">Kontot blir administratör för den här installationen. Uppgifterna lagras bara på din server.</p>
      <form onSubmit={submit} className="mt-6 space-y-4" noValidate>
        <div className="space-y-1.5">
          <Label htmlFor="name">Namn</Label>
          <Input id="name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Valfritt" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="email">E-postadress</Label>
          <Input
            id="email"
            type="email"
            autoComplete="username"
            inputMode="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={touched && !emailOk}
            aria-describedby="email-err"
          />
          <FieldError id="email-err">{touched && !emailOk ? "Ange en giltig e-postadress." : null}</FieldError>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Lösenord</Label>
          <PasswordInput
            id="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={touched && !pwOk}
            aria-describedby="pw-reqs"
          />
          <ul id="pw-reqs" className="mt-1.5 space-y-1" aria-label="Lösenordskrav">
            {checks.map((c) => (
              <li key={c.label} className={cn("flex items-center gap-1.5 text-xs", c.ok ? "text-ok-ink" : "text-fg-muted")}>
                {c.ok ? <Check className="size-3.5" aria-hidden /> : <Circle className="size-3.5" aria-hidden />}
                {c.label}
                <span className="sr-only">{c.ok ? " – uppfyllt" : " – inte uppfyllt"}</span>
              </li>
            ))}
          </ul>
          <FieldHint>Använd gärna en lösenordsfras med flera ord.</FieldHint>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="confirm">Upprepa lösenord</Label>
          <PasswordInput
            id="confirm"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            aria-invalid={touched && !match}
            aria-describedby="confirm-err"
          />
          <FieldError id="confirm-err">{(touched || confirm.length >= password.length) && confirm && !match ? "Lösenorden stämmer inte överens." : null}</FieldError>
        </div>
        {setup.error ? (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-err/30 bg-err-soft px-3 py-2 text-sm text-err-ink">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{wait > 0 ? `För många försök. Försök igen om ${wait} s.` : errorMessage(setup.error)}</span>
          </div>
        ) : null}
        <Button type="submit" variant="primary" size="lg" className="w-full" loading={setup.isPending} disabled={wait > 0}>
          {wait > 0 ? `Vänta ${wait} s` : "Skapa konto och fortsätt"}
        </Button>
      </form>
    </div>
  );
}
