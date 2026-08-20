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
export type Settings = components["schemas"]["SettingsOut"];
export type SettingsForm = components["schemas"]["SettingsFormOut"];
export type SettingsBlock = components["schemas"]["BlockOut"];
export type SettingsField = components["schemas"]["FieldOut"];
export type SystemForm = components["schemas"]["SystemFormOut"];
export type SystemModels = components["schemas"]["SystemModelsOut"];
export type SystemTest = components["schemas"]["SystemTestOut"];
export type SystemX = components["schemas"]["SystemXOut"];
export type Segment = components["schemas"]["SegmentOut"];
export type ChangeOut = components["schemas"]["ChangeOut"];
export type Funnel = components["schemas"]["FunnelOut"];
export type Favorite = components["schemas"]["FavoriteOut"];
export type Job = components["schemas"]["JobOut"];
export type JobStage = components["schemas"]["JobStageOut"];
export type OpportunityList = components["schemas"]["OpportunityListOut"];
export type OpportunityItem = components["schemas"]["OpportunityItemOut"];
export type OpportunityStep = components["schemas"]["OpportunityStepOut"];
export type OpportunitySource = components["schemas"]["OpportunitySourceOut"];
export type OpportunityScan = components["schemas"]["OpportunityScanOut"];
export type OpportunityFilter = components["schemas"]["OpportunityFilterOut"];
export type OpportunitySetting = components["schemas"]["OpportunitySettingOut"];
export type OpportunitySettings = components["schemas"]["OpportunitySettingsOut"];
export type OpportunitySettingBlock = components["schemas"]["OpportunitySettingBlockOut"];
export type OpportunitySettingGroup = components["schemas"]["OpportunitySettingGroupOut"];
export type OpportunityScanRow = components["schemas"]["OpportunityScanRowOut"];
export type OpportunitySort = components["schemas"]["OpportunitySortOut"];

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(`${status} ${detail}`);
  }
}

/** What to put on the screen when a request failed.
 *
 * The backend writes the detail of a 4xx for the operator (CLAUDE.md · 设计取舍), so that sentence is
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

/** 新机会: what the outside world is offering, found by this app's own scans.
 *
 *  One read for the whole page - entries, the counts behind its header, the last scan and the
 *  page's own copy. The entries carry their steps, so opening one is not a second request.
 *  Nothing here is scoped by a chain or a set of settings: an activity belongs to no chain,
 *  which is why this is a Place of its own rather than a tab under a radar. */
export const getOpportunity = () => request<OpportunityList>("/opportunity");

/** The watchlist: what the next scan will read, and what verified each row. */
export const listOpportunitySources = () =>
  request<OpportunitySource[]>("/opportunity/sources");

/** Start one scan and answer with the row to poll. A 409 means one is already running -
 *  the backend refuses rather than queues, because two scans read the same timelines. */
export const startOpportunityScan = () =>
  request<OpportunityScan>("/opportunity/scan", { method: "POST" });

/** 设定页的字段与分组：键、名字、当前值、范围与说明，全部由后端给。 */
export const getOpportunitySettings = () =>
  request<OpportunitySettings>("/opportunity/settings");

export const saveOpportunitySettings = (values: Record<string, number>) =>
  request<OpportunitySettings>("/opportunity/settings", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });

/** 每一轮扫描，最新在前：归档 tab 与顶栏那条时间轮次读它。 */
export const listOpportunityScans = (limit = 20) =>
  request<OpportunityScanRow[]>("/opportunity/scans?limit=" + limit);

/** 还在排队的东西：未分组 / 未抽取 / 溯源失败。 */
export const getOpportunityQueues = () =>
  request<{
    unclassified: { count: number; top: { ref: string; handle: string; text: string; score: number }[] };
    unextracted: {
      count: number;
      top: { ref: string; handle: string; text: string; score: number; project_name?: string | null }[];
    };
    trace_failed: { count: number };
  }>("/opportunity/queues");

/** 归档 / 删除 / 恢复 one entry. The page's only write, and it is the reader's own. */
export const setOpportunityState = (slug: string, userState: string) =>
  request<OpportunityItem>("/opportunity/items/" + encodeURIComponent(slug), {
    method: "PATCH",
    body: JSON.stringify({ user_state: userState }),
  });

export const getOpportunityScan = (scanId: string) =>
  request<OpportunityScan>("/opportunity/scans/" + encodeURIComponent(scanId));

/** Ask the running scan to stop. Answers `cancelled: false` when there is nothing to stop -
 *  a scan that finished between the click and the call is not an error. The scan stops at the
 *  next X call rather than at the end of one, which is the point: what it is usually waiting on
 *  is a rate limit. */
export const cancelOpportunityScan = (scanId: string) =>
  request<{ cancelled: boolean; reason: string }>(
    "/opportunity/scans/" + encodeURIComponent(scanId) + "/cancel", { method: "POST" });
