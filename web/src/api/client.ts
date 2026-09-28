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

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(`${status} ${detail}`);
  }
}

/** What to put on the screen when a request failed.
 *
 * The backend writes the detail of a 4xx for the operator (C-003), so that sentence is
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

export const listRuns = (chainId: string, limit = 20) =>
  request<Run[]>(`/runs?chain_id=${encodeURIComponent(chainId)}&limit=${limit}`);

export const getRun = (runId: string) => request<Run>(`/runs/${encodeURIComponent(runId)}`);

export const getBatch = (runId: string) =>
  request<Batch>(`/runs/${encodeURIComponent(runId)}/batch`);

export const createRun = (chainId: string, trigger = "manual") =>
  request<Run>("/runs", {
    method: "POST",
    body: JSON.stringify({ chain_id: chainId, trigger }),
  });

/** 中止: ask a round that is still going to stop. The round answers at its own next
 *  checkpoint - nothing here waits for it - so what comes back is the row as it stands, and
 *  the page's own poll is what notices the round has actually stopped. */
export const cancelRun = (runId: string) =>
  request<Run>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });

export const getSettingsForm = (chainId: string) =>
  request<SettingsForm>(`/chains/${encodeURIComponent(chainId)}/settings/form`);

export const getSettings = (chainId: string) =>
  request<Settings>(`/chains/${encodeURIComponent(chainId)}/settings`);

/** Commit a partial patch of dotted-path leaves. The backend coerces every value
 *  against the current config type, so an <input>'s string can never change a type. */
export const putSettings = (chainId: string, patch: Record<string, unknown>, note?: string) =>
  request<Settings>(`/chains/${encodeURIComponent(chainId)}/settings`, {
    method: "PUT",
    body: JSON.stringify({ patch, note }),
  });

/** Run this round now with the settings in force. */
export const runNow = (chainId: string) =>
  request<Run>(`/chains/${encodeURIComponent(chainId)}/settings/run`, { method: "POST" });

