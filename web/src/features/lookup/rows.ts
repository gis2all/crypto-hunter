import type { Favorite } from "../../api/client";

/** A lookup row's identity: one card per (chain, address), which is the shelf's own key and
 *  the pair the API is asked to forget by. Two readers key off it - the list and 搜索管理 -
 *  and a row the two computed differently would be one a star and a delete disagree about. */
export function rowKey(row: { chain_id: string; ca: string }): string {
  return row.chain_id + ":" + row.ca;
}

/** An address short enough for a list: head and tail, which is what tells two apart. */
export function shortCa(ca: string): string {
  return ca.length > 12 ? ca.slice(0, 6) + "\u2026" + ca.slice(-4) : ca;
}

/** The rows that are only records - what 搜索管理 may drop, and what 清空 would take. */
export function onlyRecords(rows: Favorite[], kept: Set<string>): Favorite[] {
  return rows.filter((row) => !kept.has(rowKey(row)));
}
