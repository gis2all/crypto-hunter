import { Star, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";

import { errorText } from "../../api/client";
import type { Chain, Favorite } from "../../api/client";
import { useFavorites, useForgetFavorite } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { Pick } from "../../components/Pick";
import { whole } from "../../format";
import { Card } from "../report/Card";
import { chainName, field, parts, stamp } from "../report/foot";

/** The one chip that names a source rather than a chain. It cannot collide with a chain id
 *  the backend would ever write, and it reads as the word the page uses for it. */
const SEARCHED = "search";

/** The shelf: every card the reader kept, across every chain.

 *  It is its own place rather than a tab under a radar (Rail, 收藏) because a kept card
 *  belongs to no single chain - it can come from a round, and later from a search on a chain
 *  this app does not screen at all. Nothing here is scoped by the selected radar or by a set
 *  of settings: the page is the same whichever radar the reader was in.
 *
 *  A row carries the card itself (see the favorites table), so this page draws a card the
 *  same way the report page does, from the payload the round was read as - deltas and all.
 */

/** The same shape as the report page's export, one card per kept row. */
function favoritesToMarkdown(rows: Favorite[], labels: Map<string, string>): string {
  const lines: string[] = ["# Crypto Hunter · 收藏", "", "共 " + rows.length + " 张", ""];
  for (const row of rows) {
    const card = row.card;
    lines.push("## " + card.sym + " · 综合分 " + whole(card.score));
    lines.push("");
    lines.push("`" + card.ca + "`");
    lines.push("");
    lines.push(parts(field("链", chainName(labels, row.chain_id)), field("配置", row.config_id)));
    lines.push(parts(field("生成时间", stamp(row.origin_label)),
      field("收藏时间", stamp(row.saved_at))));
    lines.push("");
    lines.push(card.verdict.map((segment) => segment.text).join(""));
    lines.push("");
    lines.push("| 指标 | 值 |");
    lines.push("| --- | --- |");
    for (const metric of card.metrics) {
      lines.push("| " + metric.key + " | " + metric.value + " |");
    }
    lines.push("");
    for (const link of card.links) {
      lines.push("- " + link.label + "：" + link.url);
    }
    if (card.links.length) lines.push("");
  }
  return lines.join(String.fromCharCode(10));
}

function download(rows: Favorite[], labels: Map<string, string>) {
  const blob = new Blob([favoritesToMarkdown(rows, labels)],
    { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "crypto-hunter-favorites.md";
  link.click();
  URL.revokeObjectURL(url);
}

/** Which chain a set of settings belongs to, and when the card was read and kept - the two
 *  lines the shelf prints under a card. It is the one thing a card cannot say about itself:
 *  the report page draws the round it is standing in, and a kept card has no such page.
 *
 *  生成时间 moves on its own: a round that writes this token again hands the shelf its newer
 *  card (report/assemble.py `refresh_kept`), while 收藏时间 stays at the moment the reader
 *  kept it. The two are printed together so a card that has been superseded reads as one. */
function provenance(row: Favorite, labels: Map<string, string>) {
  return (
    <>
      <span>{parts(field("链", chainName(labels, row.chain_id)), field("配置", row.config_id))}</span>
      <span>{parts(field("生成时间", stamp(row.origin_label)),
        field("收藏时间", stamp(row.saved_at)))}</span>
    </>
  );
}

export function FavoritesView({ chains }: { chains: Chain[] }) {
  const shelf = useFavorites();
  const forget = useForgetFavorite();
  // Empty means 全部. A filter and a sort are view state, not kept anywhere: the shelf is
  // ordered by when a card was kept, which is the order the API answers in.
  const [chain, setChain] = useState("");
  const [query, setQuery] = useState("");
  const [byScore, setByScore] = useState(false);

  const rows = useMemo(() => shelf.data ?? [], [shelf.data]);

  // Chain names for the band under a card, read off every chain rather than off the rail's
  // ones: a kept card can come from a chain this app does not screen.
  const labels = useMemo(() => new Map(chains.map((c) => [c.id, c.name])), [chains]);

  // The switcher's own row: the chains the rail lists, in that order, always - whether or not
  // the shelf happens to hold one of them today. A control that comes and goes with the
  // contents is a control the reader has to go looking for, and "nothing kept from this one
  // yet" is a useful answer rather than a reason to hide the button. A kept card from a chain
  // the rail does not list is appended: the card is on the shelf to be found, so it has to be
  // filterable.
  const chainIds = useMemo(() => {
    const known = chains.filter((c) => c.enabled).map((c) => c.id);
    const extra = rows.map((row) => row.chain_id).filter((id) => !known.includes(id));
    return [...known, ...new Set(extra)];
  }, [chains, rows]);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = rows.filter((row) => {
      // 搜索 is a source, not a chain: a card a CA search left behind can be from any chain
      // - including one this app does not screen - so it cannot be a chip in the chain list.
      // One at a time, like the rest: the chips are one choice, not two stacked filters.
      if (chain === SEARCHED) {
        if (row.origin !== "search") return false;
      } else if (chain && row.chain_id !== chain) {
        return false;
      }
      if (!needle) return true;
      return (row.symbol ?? "").toLowerCase().includes(needle)
        || row.ca.toLowerCase().includes(needle);
    });
    if (!byScore) return list;
    return [...list].sort((a, b) => (b.card.score ?? -1) - (a.card.score ?? -1));
  }, [rows, chain, query, byScore]);

  return (
    <div id="view-favorites">
      <div className="fav-bar">
        <div className="fav-title">
          <Icon as={Star} size={14} />
          <b>收藏</b>
        </div>
        <div className="fav-tools">
          <div className="fav-chips">
              <button type="button" className={"chip" + (chain === "" ? " on" : "")}
                onClick={() => setChain("")}>全部</button>
              {chainIds.map((id) => (
                <button type="button" key={id} className={"chip" + (chain === id ? " on" : "")}
                  onClick={() => setChain(id)}>
                  {chainName(labels, id)}
                </button>
              ))}
              {/* A card a CA search left behind is on the shelf like any other once it is
                  collected, and no chain chip can find it when its chain has no radar - so
                  the source is its own chip, standing after the chains (user, 2026-10-01). */}
              <button type="button" className={"chip" + (chain === SEARCHED ? " on" : "")}
                onClick={() => setChain(SEARCHED)}>搜索</button>
          </div>
          <input className="fav-search" value={query} placeholder="过滤 symbol / CA"
            onChange={(event) => setQuery(event.target.value)} />
          <Pick
            className="btn sec fav-sort"
            label="排序"
            current={byScore ? "score" : "time"}
            trigger={byScore ? "按分数" : "按收藏时间"}
            options={[{ value: "time", label: "按收藏时间" },
              { value: "score", label: "按分数" }]}
            onPick={(value) => setByScore(value === "score")}
          />
          {/* Locked like the topbar's 导出 and for the same reason (9.5.1): the handler is
              wired, the control says so. */}
          <button type="button" className="btn sec demo" disabled
            title="导出尚未接入（当前不做任何事）"
            onClick={() => download(shown, labels)}>导出</button>
        </div>
      </div>
      {shelf.isError ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>收藏读取失败：{errorText(shelf.error)}</span>
        </div>
      ) : shown.length ? (
        <div className="grid">
          {shown.map((row) => (
            <Card
              key={row.chain_id + ":" + row.ca}
              card={row.card}
              openAll={false}
              bulkTick={0}
              favorited
              onFavorite={() => forget.mutate({ chainId: row.chain_id, ca: row.ca })}
              foot={provenance(row, labels)}
            />
          ))}
        </div>
      ) : (
        <div className="note-bar">
          {rows.length ? "没有符合条件的收藏。" : "还没有收藏。在任意报告卡上点星标收进来。"}
        </div>
      )}
    </div>
  );
}
