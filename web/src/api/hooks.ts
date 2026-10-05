/** TanStack Query bindings. One hook per screen-level question.
 *
 *  Everything below the chain list is scoped by a set of settings as well as by a chain, and
 *  every key carries both. One chain's sets of settings keep separate report libraries and
 *  separate settings lines, so a key that named only the chain would let react-query hand
 *  one set's runs to a page that is showing another's - the page would look right and be
 *  reading a library nobody asked for. */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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

/** 信息流's three reads. No chain and no set of settings in any key, unlike everything above:
 *  a key carrying a chain would refetch the page for a context that cannot change the answer.
 *
 *  The taxonomy is cached the way the chain list is - it is a vocabulary, and a vocabulary only
 *  changes when the backend is redeployed - while the two content reads keep the window in the
 *  key so switching category or scope is a refetch, not a re-render of somebody else's rows. */
export function useFeedTaxonomy() {
  return useQuery({ queryKey: ["feed-taxonomy"], queryFn: api.getFeedTaxonomy, staleTime: Infinity });
}

/** 一页多少条. 值必须和后端的 `rules.PAGE_SIZE` 一样 —— 那是"一次读多少"的口径, 两处各写一个
 *  数, 迟早会一个 40 一个 60. 页面滚动到底再要下一页 (`next_cursor`), 所以第一屏不必背着一整
 *  个窗口的条目. */
const FEED_PAGE_SIZE = 40;

/** 全部动态 与 精选 的滚动加载: 一页 40 条, 游标读"上一页最后那一条之后". */
export function useFeedItemsPaged(scope: "all" | "curated", category: string) {
  return useInfiniteQuery({
    queryKey: ["feed-items", scope, category],
    queryFn: ({ pageParam }) => api.getFeedItems(scope, category, pageParam, FEED_PAGE_SIZE),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
  });
}

export function useFeedHot(limit = 10) {
  return useQuery({ queryKey: ["feed-hot", limit], queryFn: () => api.getFeedHot(limit) });
}

/** 主题页左栏的目录. 和词表一样按"只有后端重新部署才变"缓存: 它是名录, 不是内容.
 *  条数会随窗口移动, 所以 60 秒后允许重取一次 —— 目录里的数字不该比中栏那批落后一整天. */
export function useFeedTopics() {
  return useQuery({
    queryKey: ["feed-topics"],
    queryFn: api.getFeedTopics,
    staleTime: 60_000,
  });
}

/** 一条线上的条目. `topic` 空串时不发请求: 主题页在没有选中任何一条线时不问后端要东西,
 *  和两个详情页同一个道理. */
export function useFeedTopicItems(topic: string) {
  return useQuery({
    queryKey: ["feed-topic-items", topic],
    queryFn: () => api.getFeedTopicItems(topic),
    enabled: Boolean(topic),
  });
}

/** 一条 / 一件事的整页. `enabled` on the id because the reading pane is closed most of the time:
 *  a closed pane should not be asking the backend for anything. */
export function useFeedItem(id: string | undefined) {
  return useQuery({
    queryKey: ["feed-item", id],
    queryFn: () => api.getFeedItem(id as string),
    enabled: Boolean(id),
  });
}

export function useFeedStory(id: string | undefined) {
  return useQuery({
    queryKey: ["feed-story", id],
    queryFn: () => api.getFeedStory(id as string),
    enabled: Boolean(id),
  });
}

/** 出过的各期. 只有左栏那一列读它, 一期一行. */
export function useFeedEditions(kind: string, limit = 50) {
  return useQuery({
    queryKey: ["feed-editions", kind, limit],
    queryFn: () => api.getFeedEditions(kind, limit),
  });
}

/** 一期的正文. 和两个详情页同一个道理: 没选中任何一期时, 不向后端要东西. */
export function useFeedEdition(id: string | undefined) {
  return useQuery({
    queryKey: ["feed-edition", id],
    queryFn: () => api.getFeedEdition(id as string),
    enabled: Boolean(id),
  });
}

/** 信息流设定: 出刊节奏与两道预算. 一个装置一份, 所以键里没有链条也没有配置. */
export function useFeedSettingsForm() {
  return useQuery({ queryKey: ["feed-settings"], queryFn: api.getFeedSettings });
}

/** 存下改过的那几行, 然后拿文件现在的样子替换掉表单.
 *
 *  响应不被丢掉: 一次保存可能让某一格看起来和输入的不一样 (一个时刻被写回 08:00, 一格清成 0),
 *  页面要画的是写进去的东西. */
export function useSaveFeedSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.putFeedSettings(patch),
    onSuccess: (form) => {
      qc.setQueryData(["feed-settings"], form);
    },
  });
}
