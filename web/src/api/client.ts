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
export type Segment = components["schemas"]["SegmentOut"];
export type ChangeOut = components["schemas"]["ChangeOut"];
export type RuleCount = components["schemas"]["RuleCountOut"];
export type SourceCount = components["schemas"]["SourceCountOut"];
export type PushInfo = components["schemas"]["PushOut"];
export type Funnel = components["schemas"]["FunnelOut"];
export type Favorite = components["schemas"]["FavoriteOut"];

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
