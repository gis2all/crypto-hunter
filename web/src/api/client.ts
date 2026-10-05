/** Thin fetch wrapper over /api/v1.
 *
 * Types come from `types.gen.ts` (generated from the backend OpenAPI schema).
 * Never hand-write a DTO here: `scripts/openapi.ps1` is the only source.
 *
 * The frontend never talks to a data source or a database, and never assembles a
 * page out of many requests: one screen is one endpoint.
 */

import type { components } from "./types.gen";

export type Health = components["schemas"]["HealthOut"];
export type Chain = components["schemas"]["ChainOut"];
export type ChainConfig = components["schemas"]["ChainConfigOut"];
export type ChainConfigDetail = components["schemas"]["ChainConfigDetailOut"];
export type Library = components["schemas"]["LibraryOut"];
export type Run = components["schemas"]["RunOut"];
export type Batch = components["schemas"]["BatchOut"];
export type Card = components["schemas"]["CardOut"];
export type DeclineRow = components["schemas"]["DeclineRowOut"];
export type Settings = components["schemas"]["SettingsOut"];
export type SettingsForm = components["schemas"]["SettingsFormOut"];
export type SettingsBlock = components["schemas"]["BlockOut"];
export type SettingsGroup = components["schemas"]["GroupOut"];
export type SettingsField = components["schemas"]["FieldOut"];
export type SystemForm = components["schemas"]["SystemFormOut"];
export type SystemModels = components["schemas"]["SystemModelsOut"];
export type SystemTest = components["schemas"]["SystemTestOut"];
export type SystemX = components["schemas"]["SystemXOut"];
export type Segment = components["schemas"]["SegmentOut"];
export type ChangeOut = components["schemas"]["ChangeOut"];
export type RuleCount = components["schemas"]["RuleCountOut"];
export type SourceCount = components["schemas"]["SourceCountOut"];
export type PushInfo = components["schemas"]["PushOut"];
export type Funnel = components["schemas"]["FunnelOut"];
export type Favorite = components["schemas"]["FavoriteOut"];
export type Job = components["schemas"]["JobOut"];
export type JobStage = components["schemas"]["JobStageOut"];
export type FeedCategory = components["schemas"]["FeedCategoryOut"];
export type FeedListItem = components["schemas"]["FeedListItemOut"];
export type FeedDay = components["schemas"]["FeedDayOut"];
export type FeedItems = components["schemas"]["FeedItemsOut"];
export type FeedTopicRow = components["schemas"]["FeedTopicRowOut"];
export type FeedTopicGroup = components["schemas"]["FeedTopicGroupOut"];
export type FeedTopics = components["schemas"]["FeedTopicsOut"];
export type FeedTopicItems = components["schemas"]["FeedTopicItemsOut"];
export type FeedStoryItem = components["schemas"]["FeedStoryItemOut"];
export type FeedStory = components["schemas"]["FeedStoryOut"];
export type FeedHot = components["schemas"]["FeedHotOut"];
export type FeedTaxonomy = components["schemas"]["FeedTaxonomyOut"];
export type FeedOtherReport = components["schemas"]["FeedOtherReportOut"];
export type FeedFace = components["schemas"]["FeedFaceOut"];
export type FeedItemDetail = components["schemas"]["FeedItemDetailOut"];
export type FeedTimelineItem = components["schemas"]["FeedTimelineItemOut"];
export type FeedTimelineDay = components["schemas"]["FeedTimelineDayOut"];
export type FeedStoryDetail = components["schemas"]["FeedStoryDetailOut"];
export type FeedEdition = components["schemas"]["FeedEditionOut"];
export type FeedEditionRow = components["schemas"]["FeedEditionRowOut"];
export type FeedEditionEntry = components["schemas"]["FeedEventOut"];
export type FeedMetric = components["schemas"]["FeedMetricOut"];
export type FeedDisplayTag = components["schemas"]["FeedDisplayTagOut"];

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(`${status} ${detail}`);
  }
}

/** What to put on the screen when a request failed.
 *
 * The backend writes the detail of a 4xx for the operator (CLAUDE.md 2.3.3), so that sentence is
 * the message and the status line in front of it is noise. A 5xx detail is written for
 * nobody - "500 Internal Server Error" is a report, not advice - so it keeps its code.
 */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status >= 500 ? `HTTP ${error.status} ${error.detail}` : error.detail;
  }
  return String(error);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = (await resp.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      /* keep the status text */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export const getHealth = () => request<Health>("/health");
export const listChains = () => request<Chain[]>("/chains");

/** `?config_id=` for the reads and writes that belong to one set of settings. Always sent,
 *  including for the default one: `/runs` without it answers for every set of settings on
 *  the chain at once, which is two report libraries blended. */
function configQuery(configId: string): string {
  return `?config_id=${encodeURIComponent(configId)}`;
}

export const listRuns = (chainId: string, configId: string, limit = 20) =>
  request<Run[]>(
    `/runs?chain_id=${encodeURIComponent(chainId)}&config_id=${encodeURIComponent(configId)}`
    + `&limit=${limit}`,
  );

export const getRun = (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}`);

export const getBatch = (runId: string) =>
  request<Batch>(`/runs/${encodeURIComponent(runId)}/batch`);

export const createRun = (chainId: string, configId: string, trigger = "manual") =>
  request<Run>("/runs", {
    method: "POST",
    body: JSON.stringify({ chain_id: chainId, config_id: configId, trigger }),
  });

/** 中止: ask a round that is still going to stop. The round answers at its own next
 *  checkpoint - nothing here waits for it - so what comes back is the row as it stands, and
 *  the page's own poll is what notices the round has actually stopped. */
export const cancelRun = (runId: string) =>
  request<Run>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });

export const getSettingsForm = (chainId: string, configId: string) =>
  request<SettingsForm>(
    `/chains/${encodeURIComponent(chainId)}/settings/form${configQuery(configId)}`,
  );

export const getSettings = (chainId: string, configId: string) =>
  request<Settings>(
    `/chains/${encodeURIComponent(chainId)}/settings${configQuery(configId)}`,
  );

/** Commit a partial patch of dotted-path leaves. The backend coerces every value
 *  against the current config type, so an <input>'s string can never change a type.
 *
 *  This is also the write that makes the set of settings it names the chain's live one:
 *  the page shows which one is being edited, and saving is the only thing that can mean
 *  "and use it". */
export const putSettings = (
  chainId: string,
  configId: string,
  patch: Record<string, unknown>,
  note?: string,
) =>
  request<Settings>(`/chains/${encodeURIComponent(chainId)}/settings${configQuery(configId)}`, {
    method: "PUT",
    body: JSON.stringify({ patch, note }),
  });

/** Run this round now, under the set of settings the page is looking at. */
export const runNow = (chainId: string, configId: string) =>
  request<Run>(
    `/chains/${encodeURIComponent(chainId)}/settings/run${configQuery(configId)}`,
    { method: "POST" },
  );

/** One set of settings on a chain, with the size of the report library behind it. The size
 *  is what the delete's confirmation prints: "3 轮 / 24 张卡" is a dialog someone reads. */
export const getChainConfig = (chainId: string, configId: string) =>
  request<ChainConfigDetail>(
    `/chains/${encodeURIComponent(chainId)}/configs/${encodeURIComponent(configId)}`,
  );

/** Add a set of settings, seeded from `default` (`new`) or from a named one (`copy`). A
 *  blank label means the backend's own naming rule, which is the only place that rule is
 *  written down - the page never computes a name. */
export const addChainConfig = (
  chainId: string,
  mode: "new" | "copy",
  fromConfigId?: string,
  label?: string,
) =>
  request<ChainConfigDetail>(`/chains/${encodeURIComponent(chainId)}/configs`, {
    method: "POST",
    body: JSON.stringify({ mode, from_config_id: fromConfigId ?? null, label: label ?? null }),
  });

/** Rename one. The id behind it never moves, so nothing else on the page has to. */
export const renameChainConfig = (chainId: string, configId: string, label: string) =>
  request<ChainConfigDetail>(
    `/chains/${encodeURIComponent(chainId)}/configs/${encodeURIComponent(configId)}`,
    { method: "PATCH", body: JSON.stringify({ label }) },
  );

/** Delete one, and its whole report library with it. There is no undo.
 *
 *  `label` is the name as the operator typed it back. The backend refuses a delete whose
 *  label does not match the entry, which is what turns the confirmation from a click into
 *  something that has to be meant. */
export const deleteChainConfig = (chainId: string, configId: string, label: string) =>
  request<Library>(
    `/chains/${encodeURIComponent(chainId)}/configs/${encodeURIComponent(configId)}`,
    { method: "DELETE", body: JSON.stringify({ label }) },
  );

/** Kept cards: the shelf. Keyed by chain + CA rather than by a round, and the row carries
 *  the card itself - so a kept card still reads after the round that produced it, and its
 *  whole report library, are gone. */
export const listFavorites = () => request<Favorite[]>("/favorites");

/** Keep the card one round wrote for one token. The backend copies the assembled card. */
export const keepFavorite = (runId: string, ca: string) =>
  request<Favorite>("/favorites", {
    method: "POST",
    body: JSON.stringify({ run_id: runId, ca }),
  });

export const forgetFavorite = (chainId: string, ca: string) =>
  request<{ removed: boolean }>(
    "/favorites/" + encodeURIComponent(chainId) + "/" + encodeURIComponent(ca),
    { method: "DELETE" },
  );

/** Ask for one address to be read, on any chain. The answer is a **job**, not a card.
 *
 *  The work is minutes of network and a model call, so the backend queues it and the page
 *  watches it with `getLookupJob` - which is what lets the panel name the stage it is
 *  on and time each one. A 404 comes back before anything is queued (that string is not an
 *  address); an address the feed has never seen fails the job's own 认链 stage instead.
 *
 *  The card is left on the record the moment it is read, collected or not, which is what
 *  makes opening a past search instant and free (`listLookupRecent`). */
export const searchCa = (ca: string) =>
  request<Job>("/lookup", { method: "POST", body: JSON.stringify({ ca }) });

/** Where a queued search has got to, and the card once it has one. */
export const getLookupJob = (jobId: string) =>
  request<Job>("/lookup/jobs/" + encodeURIComponent(jobId));

/** The last search on record, whoever queued it. A page that has just opened holds no job id
 *  of its own but still has a 运行结果 worth folding, and "the newest one" is a question the
 *  server can answer without the page having remembered anything. null means nothing has been
 *  searched yet - a state, not an error. */
export const getLatestLookupJob = () => request<Job | null>("/lookup/jobs/latest");

/** The cards past searches left behind, newest first. Each row already carries its card, so
 *  opening one is a read of this list rather than a second search. */
export const listLookupRecent = () => request<Favorite[]>("/lookup/recent");

/** Drop one record off 最近搜索, and the card that came with it. Only a record: a row the
 *  reader starred is a card on the shelf and stays (see the route's own note). */
export const forgetLookup = (chainId: string, ca: string) =>
  request<{ removed: boolean }>(
    "/lookup/recent/" + encodeURIComponent(chainId) + "/" + encodeURIComponent(ca),
    { method: "DELETE" },
  );

/** Empty 最近搜索 of the records that are only records. `removed: false` when there was
 *  nothing to drop, which is one answer rather than an error. */
export const clearLookups = () =>
  request<{ removed: boolean }>("/lookup/recent", { method: "DELETE" });

/** Collect a card that is already on file. A searched token has no round to copy one from -
 *  the search wrote the card itself - so this is the star on a row that exists, and 404 is
 *  a page holding a row the database does not. */
export const collectFavorite = (chainId: string, ca: string) =>
  request<Favorite>(
    "/favorites/" + encodeURIComponent(chainId) + "/" + encodeURIComponent(ca),
    { method: "POST" },
  );

/** 系统设置: this machine's wiring - the gateway, the X session, the log level.
 *
 *  Not scoped by a chain or a set of settings: there is one of each per install, and the rows
 *  are the keys of one file (config/system.yaml). A patch is keyed by a row's own path in that
 *  file, so a save writes exactly the lines it names. */
export const getSystemForm = () => request<SystemForm>("/system/form");

export const putSystem = (patch: Record<string, unknown>) =>
  request<SystemForm>("/system", { method: "PUT", body: JSON.stringify({ patch }) });

/** Ask the endpoint what it serves. Read with the rows as they stand on screen, unsaved: a
 *  form that had to be saved before it could be tested would make the operator store an
 *  endpoint in order to find out whether it is the right one. Nothing is written. */
export const getSystemModels = (patch: Record<string, unknown>) =>
  request<SystemModels>("/system/models", { method: "POST", body: JSON.stringify({ patch }) });

/** One really small real call to the endpoint: whether it answers, and whether text comes
 *  back. Also unrecorded - it is a button, not a round. */
export const testSystem = (patch: Record<string, unknown>) =>
  request<SystemTest>("/system/test", { method: "POST", body: JSON.stringify({ patch }) });

/** The X session's own probe: one read of the account the cookies claim to be, which is the
 *  cheapest question that tells a session from two strings that look like one. Asked with the
 *  page's unsaved rows, and unrecorded - a search spends a rate budget a round needs. */
export const probeXSession = (patch: Record<string, unknown>) =>
  request<SystemX>("/system/x", { method: "POST", body: JSON.stringify({ patch }) });

/** 信息流: the macro feed - the industry's own news, not one chain's tokens.
 *
 *  Nothing below is scoped by a chain or by a set of settings, and that is not an omission: the
 *  feed reads the industry, and the industry is not on one chain. It is also why these live
 *  beside the radar's calls instead of under them - the page that reads them is a place of its
 *  own (Rail, 信息流).
 *
 *  Three reads for three pages, and they are three questions about one body of items rather than
 *  three datasets: 全部动态 asks what the window holds, 精选 asks which of it cleared the bar,
 *  and 热点榜 asks which *stories* the window's items add up to. Nothing here takes a chain, a
 *  set of settings or a window - all three are the backend's to decide.
 */
export const getFeedTaxonomy = () => request<FeedTaxonomy>("/feed/taxonomy");

/** 全部动态 (scope=all) and 精选 (scope=curated) are one route: same rows, one filter apart.
 *
 *  `cursor` 是"上一页最后那一条之后"的位置 (后端 lib/cursor), 不是页码 —— 这一秒新来一条,
 *  页码会让后面每一页都错位一次. `pageSize = 0` 是"一次给全", 页面滚动加载时才传 40.
 */
export const getFeedItems = (scope: "all" | "curated", category = "",
                             cursor = "", pageSize = 0) =>
  request<FeedItems>(`/feed/items?scope=${scope}`
    + (category ? `&category=${encodeURIComponent(category)}` : "")
    + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "")
    + (pageSize ? `&page_size=${pageSize}` : ""));

/** 热点榜. The ranking is the backend's: "how many parties are saying it" is not a number the
 *  page can see, so it is not a number the page gets to compute. */
export const getFeedHot = (limit = 10) => request<FeedHot>(`/feed/hot?limit=${limit}`);

/** 主题页左栏的目录: 实体分组, 每组带一个窗口内的条数.
 *
 *  条数是"这条线在窗口里有多少条", 窗口的长短在后端 (views.TOPIC_HOURS), 所以页面拿到的是
 *  `window_days` 这个现成的数, 不是自己写死的 30.
 */
export const getFeedTopics = () => request<FeedTopics>("/feed/topics");

/** 一条线上的条目, 按天分组 —— 主题页中栏那一列.
 *
 *  走查询串而不是路径段: `BNB Chain` 这样的名字里有空格, 而路径段里的空格每一种客户端都会
 *  各自决定怎么转义一次.
 */
export const getFeedTopicItems = (topic: string) =>
  request<FeedTopicItems>(`/feed/topics/items?topic=${encodeURIComponent(topic)}`);

/** 一条的整页. The list row above it is a summary of this, not a substitute for it: a feed that
 *  never shows the source's own words is asking to be believed rather than read.
 *
 *  By id rather than "whatever the list happened to hand down": the detail page prints things no
 *  list payload holds, and a page that draws only what it was handed grows one shape per entry
 *  point - three lists, three detail pages.
 */
export const getFeedItem = (id: string) =>
  request<FeedItemDetail>(`/feed/item?id=${encodeURIComponent(id)}`);

/** 一件事的整页. The rank comes down with it: which place a story holds is the ranking's own
 *  answer and the ranking is the backend's (2.3.4). */
export const getFeedStory = (id: string) =>
  request<FeedStoryDetail>(`/feed/story?id=${encodeURIComponent(id)}`);

/** 出过的各期, 按刊取. 三种刊是三份档案, 所以 kind 是必填的那一半. 新的在前, 一行只画日期
 *  和条数 —— 所以列表不带正文. */
export const getFeedEditions = (kind: string, limit = 50) =>
  request<FeedEditionRow[]>(`/feed/editions?kind=${kind}&limit=${limit}`);

/** 一期的整份. 按 id 取, 不是"把列表那行摊开": 一期是出刊那一刻的定稿, 它比一行多出正文. */
export const getFeedEdition = (id: string) =>
  request<FeedEdition>(`/feed/editions/${encodeURIComponent(id)}`);

/** 信息流设定: 出刊节奏与两道预算 (8.6 / 3.4).
 *
 *  和 系统设置 同一套门面 (8.9): 文件是唯一真相, 页面只是它的表 —— patch 的键就是各行在文件里
 *  的路径, PUT 回来的是文件现在的样子, 不是一句「已保存」. 它答的形状因此和 系统设置 是同一个,
 *  两个页面也就由同一套渲染器画.
 */
export const getFeedSettings = () => request<SystemForm>("/feed/settings");

export const putFeedSettings = (patch: Record<string, unknown>) =>
  request<SystemForm>("/feed/settings", { method: "PUT", body: JSON.stringify({ patch }) });
