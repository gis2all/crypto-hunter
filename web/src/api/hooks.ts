/** TanStack Query bindings. One hook per screen-level question.
 *
 *  Everything below the chain list is scoped by a set of settings as well as by a chain, and
 *  every key carries both. One chain's sets of settings keep separate report libraries and
 *  separate settings lines, so a key that named only the chain would let react-query hand
 *  one set's runs to a page that is showing another's - the page would look right and be
 *  reading a library nobody asked for. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as api from "./client";
import type { Run } from "./client";

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

/** 系统设置's one read. No chain, no set of settings: the rows are this machine's own file. */
export function useSystemForm() {
  return useQuery({ queryKey: ["system-form"], queryFn: api.getSystemForm });
}

/** Save the rows that changed, and answer the file as it now reads.
 *
 *  The response replaces the form rather than being thrown away: a save can leave a row looking
 *  different from what was typed - a credential standing as a fingerprint, a box cleared back to
 *  its default - and the page has to draw what was written. */
export function useSaveSystem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.putSystem(patch),
    onSuccess: (form) => {
      qc.setQueryData(["system-form"], form);
    },
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

/** Start a round now. `onStarted` answers the round that was just created: the page
 *  points its pager at it and opens the panel, otherwise a reader paged back to an
 *  older round keeps looking at that round and the click looks dead. */
export function useRunNow(chainId: string, configId: string, onStarted?: (run: Run) => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.runNow(chainId, configId),
    onSuccess: (run) => {
      void qc.invalidateQueries({ queryKey: ["runs", chainId, configId] });
      onStarted?.(run);
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

/** The CA search page: the cards past searches left behind, and the one slow write that adds
 *  to them. A search is not scoped by a chain or by a set of settings - the chain comes off
 *  the address - so neither key carries one. */
export function useLookupRecent() {
  return useQuery({ queryKey: ["lookup-recent"], queryFn: api.listLookupRecent });
}

/** The two writes 搜索管理 makes. One key to invalidate: the list is what they redraw, and a
 *  record is not a favourite, so the shelf has nothing to say about either of them - a row
 *  that is both is out of what they touch in the first place (api/v1/lookup.py). */
export function useForgetLookup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ chainId, ca }: { chainId: string; ca: string }) =>
      api.forgetLookup(chainId, ca),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["lookup-recent"] }),
  });
}

export function useClearLookups() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.clearLookups(),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["lookup-recent"] }),
  });
}

/** One address in, one job out. The job is put in the cache under its own key on the way
 *  past, so `useLookupJob` has something to draw before its first poll answers. */
export function useSearchCa() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ca: string) => api.searchCa(ca),
    onSuccess: (job) => {
      qc.setQueryData(["lookup-job", job.id], job);
      // The fallback reading below is now a job behind, so it is written over rather than
      // refetched: 运行结果 must fold the search just run, not the one before it.
      qc.setQueryData(["lookup-job-latest"], job);
    },
  });
}

/** A queued search, polled until it stops moving.
 *
 *  A search is minutes long and the panel is drawn from the stage list the job carries, so
 *  this asks again while it runs. `failed` ends the poll as much as `done` does: a job
 *  that died keeps the stage it died on, and folding it away would hide the answer. A job
 *  with no reading yet counts as running, because that is the state it was just handed. */
export function useLookupJob(jobId: string | null) {
  return useQuery({
    queryKey: ["lookup-job", jobId],
    queryFn: () => api.getLookupJob(jobId as string),
    enabled: Boolean(jobId),
    // An id the backend has never heard of ends the poll as well: asking again cannot
    // change a 404, and a restored id from a wiped database would otherwise be asked for
    // every `RUNNING_MS` for as long as the page is open.
    refetchInterval: (query) => {
      if (query.state.error) {
        return false;
      }
      return (query.state.data?.status ?? "running") === "running" ? RUNNING_MS : false;
    },
  });
}

/** The newest search on record, for a page that holds no job id of its own.
 *
 *  Not polled, on purpose: this is a fallback reading, and the one job that can still be
 *  moving - the page own - is polled by `useLookupJob` above. null when nothing has ever
 *  been searched, which the panel reads as "nothing to fold" rather than "queued". */
export function useLatestLookupJob() {
  return useQuery({ queryKey: ["lookup-job-latest"], queryFn: api.getLatestLookupJob });
}

/** Collecting a card that is already on file - the star on a searched token. Like
 *  `useKeepFavorite` it invalidates the shelf, which is what both readers of `kept` draw. */
export function useCollectFavorite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ chainId, ca }: { chainId: string; ca: string }) =>
      api.collectFavorite(chainId, ca),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["favorites"] }),
  });
}

/** 新机会: one read for the whole page, polled while a scan is running.
 *
 *  The scan's progress is part of this answer rather than a query of its own: the page draws
 *  the two lists and the stage list from the same row, and a second key would let them
 *  disagree about which scan they are describing. Nothing is scoped by chain or by set of
 *  settings - an activity belongs to neither. */
export function useOpportunity() {
  return useQuery({
    queryKey: ["opportunity"],
    queryFn: api.getOpportunity,
    refetchInterval: (query) =>
      (query.state.data?.scan?.status ?? "idle") === "running" ? RUNNING_MS : false,
  });
}

/** The watchlist. Not polled: it changes when an operator changes it, not when a scan runs. */
export function useOpportunitySources() {
  return useQuery({
    queryKey: ["opportunity-sources"],
    queryFn: api.listOpportunitySources,
    staleTime: 60_000,
  });
}

/** Start a scan. The answer is the scan row, written into the cache on the way past so the
 *  panel has something to draw before `useOpportunity`'s next poll lands - and the page's own
 *  read is invalidated, because the row it is holding is now one scan behind. */
export function useOpportunitySettings() {
  return useQuery({ queryKey: ["opportunity", "settings"], queryFn: api.getOpportunitySettings });
}

export function useSaveOpportunitySettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, number>) => api.saveOpportunitySettings(values),
    onSuccess: () => client.invalidateQueries({ queryKey: ["opportunity"] }),
  });
}

export function useOpportunityScans() {
  return useQuery({ queryKey: ["opportunity", "scans"], queryFn: () => api.listOpportunityScans() });
}

export function useOpportunityQueues() {
  return useQuery({
    queryKey: ["opportunity", "queues"],
    queryFn: api.getOpportunityQueues,
    refetchInterval: 60_000,
  });
}

export function useSetOpportunityState() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, state }: { slug: string; state: string }) =>
      api.setOpportunityState(slug, state),
    onSuccess: () => client.invalidateQueries({ queryKey: ["opportunity"] }),
  });
}

export function useCancelOpportunityScan() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (scanId: string) => api.cancelOpportunityScan(scanId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["opportunity"] }),
  });
}


export function useStartOpportunityScan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.startOpportunityScan(),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["opportunity"] }),
  });
}
