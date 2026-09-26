/** TanStack Query bindings. One hook per screen-level question. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as api from "./client";

export function useChains() {
  return useQuery({ queryKey: ["chains"], queryFn: api.listChains, staleTime: 60_000 });
}

export function useRuns(chainId: string) {
  return useQuery({
    queryKey: ["runs", chainId],
    queryFn: () => api.listRuns(chainId, 30),
    enabled: Boolean(chainId),
  });
}

export function useBatch(runId: string | undefined) {
  return useQuery({
    queryKey: ["batch", runId],
    queryFn: () => api.getBatch(runId as string),
    enabled: Boolean(runId),
  });
}

/** How often the open round is asked again. `settling` is slower on purpose: the round has
 *  published and nothing is being computed - an entry is being read - and the page only has
 *  to notice the moment the researched words land on the cards. */
export type RunPhase = "off" | "running" | "settling";

const RUNNING_MS = 1500;
const SETTLING_MS = 5000;

export function useLiveRun(runId: string | undefined, phase: RunPhase) {
  const interval = phase === "running" ? RUNNING_MS : phase === "settling" ? SETTLING_MS : false;
  return useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(runId as string),
    enabled: Boolean(runId) && interval !== false,
    refetchInterval: interval,
  });
}

export function useSettingsForm(chainId: string) {
  return useQuery({
    queryKey: ["settings-form", chainId],
    queryFn: () => api.getSettingsForm(chainId),
    enabled: Boolean(chainId),
  });
}

export function useSaveSettings(chainId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.putSettings(chainId, patch),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["settings-form", chainId] });
      void qc.invalidateQueries({ queryKey: ["settings", chainId] });
    },
  });
}

/** 中止, on the round the panel is showing. Both the list and the single row are invalidated
 *  on success: the panel reads the round's own state, so a click never has to guess where the
 *  round got to before it stopped. */
export function useCancelRun(chainId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.cancelRun(runId),
    onSuccess: (_row, runId) => {
      void qc.invalidateQueries({ queryKey: ["runs", chainId] });
      void qc.invalidateQueries({ queryKey: ["run", runId] });
    },
  });
}

export function useRunNow(chainId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.runNow(chainId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["runs", chainId] });
    },
  });
}
