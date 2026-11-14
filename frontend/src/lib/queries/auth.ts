"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setCsrfToken } from "../api";
import type { SessionOut } from "../types";
import { qk } from "./keys";

/** Aktuell session. 401 ger `null` (ingen omdirigering) så att skalet kan avgöra. */
export function useMe() {
  return useQuery({
    queryKey: qk.auth.me,
    queryFn: async () => {
      try {
        const s = await api.get<SessionOut>("/auth/me", { allow401: true });
        setCsrfToken(s.csrf_token);
        return s;
      } catch (err) {
        if (err instanceof Error && "status" in err && (err as { status: number }).status === 401) return null;
        throw err;
      }
    },
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useSetupStatus() {
  return useQuery({
    queryKey: qk.auth.setupStatus,
    queryFn: () => api.get<{ needs_setup: boolean }>("/auth/setup-status", { allow401: true }),
    staleTime: 30_000,
  });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string }) =>
      api.post<SessionOut>("/auth/login", body, { allow401: true }),
    onSuccess: (s) => {
      setCsrfToken(s.csrf_token);
      qc.setQueryData(qk.auth.me, s);
    },
  });
}

export function useSetup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; password: string; display_name: string }) =>
      api.post<SessionOut>("/auth/setup", body, { allow401: true }),
    onSuccess: (s) => {
      setCsrfToken(s.csrf_token);
      qc.setQueryData(qk.auth.me, s);
      qc.setQueryData(qk.auth.setupStatus, { needs_setup: false });
    },
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<void>("/auth/logout", {}, { allow401: true }),
    onSettled: () => {
      setCsrfToken(null);
      qc.clear();
      window.location.assign("/login");
    },
  });
}
