import {
  ArrowDownAZ, ArrowDownWideNarrow, ArrowUpAZ, ArrowUpNarrowWide, Clock, FoldVertical, Gauge,
  Layers, Search, Star, TriangleAlert, UnfoldVertical,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { errorText } from "../../api/client";
import type { Chain, Favorite } from "../../api/client";
import { useFavorites, useForgetFavorite, useForgetMacroFavorite, useMacroFavorites } from "../../api/hooks";
import { zh } from "../macro/MacroView";
import { Icon } from "../../components/Icon";
import { chainIdMark } from "../../components/chainMark";
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

/** 来源 / 排序, and the direction under the sort: the shelf's controls, standing in the
 *  first track the way 最近搜索 does on the 搜索 page (2026-10-02, user). The chips filter by
 *  where a card came from - a chain, or 搜索 for a CA that came back from anywhere - and the
 *  sort is a list rather than a dropdown because it is four keys and two directions. */
type SortKey = "saved" | "origin" | "score" | "name";
type SortDir = "asc" | "desc";

/** The first direction is the one a key opens on, so a key is never shown with its second
 *  choice highlighted: 时间 opens on 从新到旧, 分数 on 从高到低, 名称 on A–Z.
 *
 *  生成时间 and 收藏时间 are the pair the card's own foot prints (provenance): a round that
 *  writes this token again hands the shelf a newer card while the moment the reader kept it
 *  stands still, so the two orders answer two different questions.
 *
 *  Every row carries its glyph here rather than in the view (2026-10-02, user): a key and its
 *  two directions are one fact, so the icon that says 收藏时间 and the pair that says 从新到旧
 *  are declared on the same line. 收藏时间 wears the star the rail and the card's own control
 *  wear for the same word, and the directions wear the arrow the sort reads with - the two
 *  keys that count a quantity share one pair, and 名称 says the alphabet it sorts by. */
const SORTS: {
  value: SortKey;
  label: string;
  icon: LucideIcon;
  dirs: [SortDir, string, LucideIcon][];
}[] = [
  { value: "saved", label: "收藏时间", icon: Star,
    dirs: [["desc", "从新到旧", ArrowDownWideNarrow], ["asc", "从旧到新", ArrowUpNarrowWide]] },
  { value: "origin", label: "生成时间", icon: Clock,
    dirs: [["desc", "从新到旧", ArrowDownWideNarrow], ["asc", "从旧到新", ArrowUpNarrowWide]] },
  { value: "score", label: "分数", icon: Gauge,
    dirs: [["desc", "从高到低", ArrowDownWideNarrow], ["asc", "从低到高", ArrowUpNarrowWide]] },
  { value: "name", label: "名称", icon: ArrowDownAZ,
    dirs: [["asc", "A–Z", ArrowDownAZ], ["desc", "Z–A", ArrowUpAZ]] },
];


/** The event half of the shelf. Coin cards stay on the shelf query above: one tab
 *  per half, and a kept event is never a kept coin card, so neither list can show the
 *  other's rows. */
function EventsShelf() {
  const shelf = useMacroFavorites();
  const forget = useForgetMacroFavorite();
  if (shelf.isPending) {
    return <div className="note-bar">{zh(0x52a0, 0x8f7d, 0x4e2d) + "..."}</div>;
  }
  if (shelf.isError) {
    return <div className="note-bar bad">{errorText(shelf.error)}</div>;
  }
  const rows = shelf.data ?? [];
  if (!rows.length) {
    return (
      <div className="note-bar">
        {zh(0x6682, 0x65e0, 0x6536, 0x85cf, 0x7684, 0x4e8b, 0x4ef6)}
      </div>
    );
  }
  return (
    <div className="macro-list">
      {rows.map((event) => (
        <article key={event.id} className="macro-item">
          <div className="macro-title">{event.title}</div>
          <div className="macro-meta">
            <span>{zh(0x70ed, 0x5ea6)} {event.heat.toFixed(1)}</span>
            <span>{event.sources_count}{zh(0x5bb6, 0x6765, 0x6e90)}</span>
            <span>{event.items_count}{zh(0x6761, 0x76f8, 0x5173, 0x62a5, 0x9053)}</span>
            <button type="button" className="btn sec"
              onClick={() => forget.mutate(event.id)}>
              <Icon as={Star} size={14} />
              {zh(0x53d6, 0x6d88, 0x6536, 0x85cf)}
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}

export function FavoritesView({ chains }: { chains: Chain[] }) {
  const shelf = useFavorites();
  const forget = useForgetFavorite();
  // Empty means 全部. A filter and a sort are view state, not kept anywhere: the shelf
  // arrives in the order it was kept (the API's own), which is exactly 收藏时间 / 从新到旧,
  // so the defaults below are the page not asking for anything.
  const [kind, setKind] = useState<"coins" | "events">("coins");
  const [chain, setChain] = useState("");
  const [query, setQuery] = useState("");
  const [key, setKey] = useState<SortKey>("saved");
  const [dir, setDir] = useState<SortDir>("desc");
  /** 展开卡片 / 收起卡片 reaches every card at once - the same bulk edit a radar bar and
   *  the 搜索 page make (8.3.7). The folds are uncontrolled <details>, so the tick is what
   *  remounts them and a manual toggle keeps working until the next click. */
  const [bulk, setBulk] = useState({ open: false, tick: 0 });

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

    const flip = dir === "asc" ? 1 : -1;
    // Two cards that compare equal keep one fixed order (CA), so the two directions are two
    // readings of the same list rather than two different shuffles of the equal ones.
    const tie = (a: Favorite, b: Favorite) =>
      a.ca.localeCompare(b.ca) || a.chain_id.localeCompare(b.chain_id);
    const cmp = (a: Favorite, b: Favorite): number => {
      if (key === "score") {
        const x = a.card.score ?? null;
        const y = b.card.score ?? null;
        // A card with no score is not a card on 0 (8.8.2) - a searched token has no pool to
        // be a percentile of - so it sinks in both directions instead of leading 从低到高.
        if (x === null || y === null) return x === y ? tie(a, b) : x === null ? 1 : -1;
        return flip * (x - y) || tie(a, b);
      }
      if (key === "name") {
        const x = a.symbol ?? a.card.sym;
        const y = b.symbol ?? b.card.sym;
        return flip * x.localeCompare(y) || tie(a, b);
      }
      const x = key === "saved" ? a.saved_at : a.origin_label;
      const y = key === "saved" ? b.saved_at : b.origin_label;
      return flip * x.localeCompare(y) || tie(a, b);
    };
    return [...list].sort(cmp);
  }, [rows, chain, query, key, dir]);

  const sortNow = SORTS.find((sort) => sort.value === key) ?? SORTS[0];

  return (
    <div id="view-favorites">
      <div className="fav-bar">
        <div className="fav-title">
          <Icon as={Star} size={14} />
          <b>收藏</b>
        </div>
        <div className="fav-tools">
          {/* The same bulk edit a radar bar makes (8.3.7). One label, two states, so the
              reader learns it once and it always says what the next click does. */}
          <button className="btn sec"
            onClick={() => setBulk((prev) => ({ open: !prev.open, tick: prev.tick + 1 }))}>
            <Icon as={bulk.open ? FoldVertical : UnfoldVertical} size={14} />
            {bulk.open ? "收起卡片" : "展开卡片"}
          </button>
          {/* The box stays in the bar rather than moving into the panel: it is a filter like
              the chips, but it is typed into and answered as it is typed, and that belongs
              beside 导出 rather than under a heading (2026-10-02, user). */}
          <input className="fav-search" value={query} placeholder="过滤 symbol / CA"
            onChange={(event) => setQuery(event.target.value)} />
          {/* Locked like the topbar's 导出 and for the same reason (9.5.1): the handler is
              wired, the control says so. */}
          <button type="button" className="btn sec demo" disabled
            title="导出尚未接入（当前不做任何事）"
            onClick={() => download(shown, labels)}>导出</button>
        </div>
      </div>
      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={kind === "coins"}
          className={"tab" + (kind === "coins" ? " on" : "")}
          onClick={() => setKind("coins")}>
          {zh(0x5e01, 0x5361)}
        </button>
        <button type="button" role="tab" aria-selected={kind === "events"}
          className={"tab" + (kind === "events" ? " on" : "")}
          onClick={() => setKind("events")}>
          {zh(0x4e8b, 0x4ef6)}
        </button>
      </div>
      {kind === "events" ? <EventsShelf /> : shelf.isError ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>收藏读取失败：{errorText(shelf.error)}</span>
        </div>
      ) : (
        /* The controls stand in the first track, the way 最近搜索 stands on the 搜索 page.
           The panel keeps its width whether or not the shelf holds anything, so the cards
           beside it do not move when the last card is unstarred. */
        <div className="grid fav-body">
          <aside className="fav-side">
            {/* One list per group, 来源 and 排序 alike (2026-10-02, user): the segmented row
                becomes one more list, so the two read the same and a chain the rail does not
                list is one more row instead of a chip that has to fit in the row above.

                Each row opens with its own glyph (2026-10-02, user): a 来源 row wears what the
                rail wears for that source - the chain's own mark, or 搜索's own glyph - and a
                排序 row wears its key or the direction it reads in. */}
            <h4 className="look-h">来源</h4>
            <div className="fav-opts">
              <button type="button" className={"fav-opt" + (chain === "" ? " on" : "")}
                onClick={() => setChain("")}>
                <Icon as={Layers} size={14} />
                全部
              </button>
              {chainIds.map((id) => (
                <button type="button" key={id} className={"fav-opt" + (chain === id ? " on" : "")}
                  onClick={() => setChain(id)}>
                  {chainIdMark(id)}
                  {chainName(labels, id)}
                </button>
              ))}
              {/* A card a CA search left behind is on the shelf like any other once it is
                  collected, and no chain row can find it when its chain has no radar - so the
                  source is its own row, standing after the chains (user, 2026-10-01). */}
              <button type="button" className={"fav-opt" + (chain === SEARCHED ? " on" : "")}
                onClick={() => setChain(SEARCHED)}>
                <Icon as={Search} size={14} />
                搜索
              </button>
            </div>
            <h4 className="look-h">排序</h4>
            <div className="fav-opts">
              {SORTS.map((sort) => (
                <button type="button" key={sort.value}
                  className={"fav-opt" + (key === sort.value ? " on" : "")}
                  onClick={() => { setKey(sort.value); setDir(sort.dirs[0][0]); }}>
                  <Icon as={sort.icon} size={14} />
                  {sort.label}
                </button>
              ))}
            </div>
            {/* The direction reads as the sort's own second half rather than as two more keys:
                it is fenced off from the four and it says what the key above it means. It is
                always two rows, so picking another sort cannot make the panel jump. */}
            <div className="fav-opts fav-dirs">
              {sortNow.dirs.map(([value, label, icon]) => (
                <button type="button" key={value}
                  className={"fav-opt" + (dir === value ? " on" : "")}
                  onClick={() => setDir(value)}>
                  <Icon as={icon} size={14} />
                  {label}
                </button>
              ))}
            </div>
          </aside>
          <section className="fav-cards">
            {shown.length ? (
              <div className="grid">
                {shown.map((row) => (
                  <Card
                    key={row.chain_id + ":" + row.ca}
                    card={row.card}
                    openAll={bulk.open}
                    bulkTick={bulk.tick}
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
          </section>
        </div>
      )}
    </div>
  );
}
