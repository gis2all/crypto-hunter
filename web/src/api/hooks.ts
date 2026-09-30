/** TanStack Query bindings. One hook per screen-level question.
 *
 *  Everything below the chain list is scoped by a set of settings as well as by a chain, and
 *  every key carries both. One chain's sets of settings keep separate report libraries and
 *  separate settings lines, so a key that named only the chain would let react-query hand
 *  one set's runs to a page that is showing another's - the page would look right and be
 *  reading a library nobody asked for. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as api from "./client";

export function useChains() {
  return useQuery({ queryKey: ["chains"], queryFn: api.listChains, staleTime: 60_000 });
}

export function useRuns(chainId: string, configId: string) {
  return useQuery({
    queryKey: ["runs", chainId, configId],
    queryFn: () => api.listRuns(chainId, configId, 30),
    enabled: Boolean(chainId && configId),
  });
}

export function useBatch(runId: string | undefined) {
  return useQuery({
    queryKey: ["batch", runId],
    queryFn: () => api.getBatch(runId as string),
    enabled: Boolean(runId),
  });
}

/** How often the open round is asked again while it is still moving. A round that has
 *  reached a terminal state is never polled: nothing will change on it again. */
export type RunPhase = "off" | "running";

const RUNNING_MS = 1500;

export function useLiveRun(runId: string | undefined, phase: RunPhase) {
  const interval = phase === "running" ? RUNNING_MS : false;
  return useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(runId as string),
    enabled: Boolean(runId) && interval !== false,
    refetchInterval: interval,
  });
}

export function useSettingsForm(chainId: string, configId: string) {
  return useQuery({
    queryKey: ["settings-form", chainId, configId],
    queryFn: () => api.getSettingsForm(chainId, configId),
    enabled: Boolean(chainId && configId),
  });
}

/** Save the draft, and make this set of settings the live one. `chains` is refetched too:
 *  the flag that says which set is live moved, and the rail and the switcher read it from
 *  there. */
export function useSaveSettings(chainId: string, configId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.putSettings(chainId, configId, patch),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["settings-form", chainId, configId] });
      void qc.invalidateQueries({ queryKey: ["settings", chainId, configId] });
      void qc.invalidateQueries({ queryKey: ["chains"] });
    },
  });
}

/** 中止, on the round the panel is showing. Both the list and the single row are invalidated
 *  on success: the panel reads the round's own state, so a click never has to guess where the
 *  round got to before it stopped. */
export function useCancelRun(chainId: string, configId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.cancelRun(runId),
    onSuccess: (_row, runId) => {
      void qc.invalidateQueries({ queryKey: ["runs", chainId, configId] });
      void qc.invalidateQueries({ queryKey: ["run", runId] });
    },
  });
}

export function useRunNow(chainId: string, configId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.runNow(chainId, configId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["runs", chainId, configId] });
    },
  });
}

/** One set of settings, with the size of its report library. Asked for when a delete is
 *  being confirmed, so the dialog can print what is about to go. */
export function useChainConfig(chainId: string, configId: string | undefined) {
  return useQuery({
    queryKey: ["chain-config", chainId, configId],
    queryFn: () => api.getChainConfig(chainId, configId as string),
    enabled: Boolean(chainId) && Boolean(configId),
  });
}

/** The three writes that change *which* sets of settings a chain has. Each one rewrites the
 *  chain's file, so the chain list - the one payload that carries them - is what has to be
 *  refetched; nothing below it can be cached longer than the list it came from. */
export function useAddChainConfig(chainId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { mode: "new" | "copy"; fromConfigId?: string; label?: string }) =>
      api.addChainConfig(chainId, body.mode, body.fromConfigId, body.label),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["chains"] }),
  });
}

export function useRenameChainConfig(chainId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { configId: string; label: string }) =>
      api.renameChainConfig(chainId, body.configId, body.label),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["chains"] }),
  });
}

export function useDeleteChainConfig(chainId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { configId: string; label: string }) =>
      api.deleteChainConfig(chainId, body.configId, body.label),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["chains"] }),
  });
}

/** The shelf. One query for both readers of it: the 收藏 page draws the cards, and a report
 *  page only needs to know which of its CAs are already on it - the same rows answer both, so
 *  a star and the shelf can never disagree. */
export function useFavorites() {
  return useQuery({ queryKey: ["favorites"], queryFn: api.listFavorites, staleTime: 30_000 });
}

/** Keeping a card. Both writes invalidate the one list: the star on a report card and the
 *  shelf are drawn from it. */
export function useKeepFavorite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, ca }: { runId: string; ca: string }) => api.keepFavorite(runId, ca),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["favorites"] }),
  });
}

export function useForgetFavorite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ chainId, ca }: { chainId: string; ca: string }) =>
      api.forgetFavorite(chainId, ca),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["favorites"] }),
  });
}
