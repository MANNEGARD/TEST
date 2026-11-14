"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertCircle, Timer } from "lucide-react";
import { PasswordInput } from "@/components/auth/password-field";
import { retryUntil, useCountdown } from "@/components/auth/rate-limit";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ApiError, errorMessage } from "@/lib/api";
import { useLogin, useSetupStatus } from "@/lib/queries/auth";

function safeNext(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/login")) return "/";
  return next;
}

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const setup = useSetupStatus();
  const login = useLogin();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [retryAfter, setRetryAfter] = React.useState<number | null>(null);
  const wait = useCountdown(retryAfter);

  React.useEffect(() => {
    if (setup.data?.needs_setup) router.replace("/setup");
  }, [setup.data, router]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (wait > 0) return;
    login.mutate(
      { email, password },
      {
        onSuccess: () => router.replace(next),
        onError: (err) => {
          if (err instanceof ApiError && err.status === 429) setRetryAfter(retryUntil(err.retryAfterSeconds));
        },
      },
    );
  };

  const error = login.error;
  const rateLimited = error instanceof ApiError && error.status === 429;

  return (
    <div className="rounded-xl border border-border bg-surface p-6 shadow-md sm:p-8">
      <h1 className="text-xl font-semibold tracking-tight">Logga in</h1>
      <p className="mt-1 text-sm text-fg-muted">Välkommen tillbaka till din körjournal.</p>
      <form onSubmit={submit} className="mt-6 space-y-4" noValidate>
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
            aria-invalid={!!error && !rateLimited}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Lösenord</Label>
          <PasswordInput
            id="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={!!error && !rateLimited}
          />
        </div>
        {error ? (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-err/30 bg-err-soft px-3 py-2 text-sm text-err-ink">
            {rateLimited ? <Timer className="mt-0.5 size-4 shrink-0" /> : <AlertCircle className="mt-0.5 size-4 shrink-0" />}
            <span>
              {rateLimited
                ? wait > 0
                  ? `För många inloggningsförsök. Försök igen om ${wait} s.`
                  : "Du kan försöka igen nu."
                : errorMessage(error)}
            </span>
          </div>
        ) : null}
        <Button type="submit" variant="primary" size="lg" className="w-full" loading={login.isPending} disabled={!email || !password || wait > 0}>
          {wait > 0 ? `Vänta ${wait} s` : "Logga in"}
        </Button>
      </form>
    </div>
  );
}
