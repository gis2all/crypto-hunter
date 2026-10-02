import { useState } from "react";
import { Flame, Star } from "lucide-react";

import { errorText } from "../../api/client";
import type { MacroEvent, MacroItem } from "../../api/client";
import {
  useForgetMacroFavorite, useKeepMacroFavorite, useMacroDigest, useMacroEvent,
  useMacroEvents, useMacroFavorites, useMacroItems, useMacroSearch, useMacroTopics,
  useTriggerMacroCollect,
} from "../../api/hooks";
import { Icon } from "../../components/Icon";

/** ASCII-safe Chinese: the shell mangles raw non-ASCII, so labels are code points. */
/** Shared with the rail and the shelf: labels without raw non-ASCII. */
export const zh = (...codes: number[]): string => String.fromCharCode(...codes);

export type MacroTab = "picked" | "items" | "events" | "digest" | "topics" | "search";

export const MACRO_TABS: { id: MacroTab; label: string }[] = [
  { id: "picked", label: zh(0x7cbe, 0x9009) },
  { id: "items", label: zh(0x5168, 0x90e8, 0x52a8, 0x6001) },
  { id: "events", label: zh(0x70ed, 0x70b9, 0x699c) },
  { id: "digest", label: zh(0x65e5, 0x62a5) },
  { id: "topics", label: zh(0x4e3b, 0x9898) },
  { id: "search", label: zh(0x641c, 0x7d22) },
];

const LOADING = zh(0x52a0, 0x8f7d, 0x4e2d) + "...";
const READ_ORIGINAL = zh(0x67e5, 0x770b, 0x539f, 0x6587);

function stamp(raw: string | null): string {
  if (!raw) return "";
  return raw.slice(0, 16).replace("T", " ");
}

function ItemRow({ item, dim }: { item: MacroItem; dim?: boolean }) {
  return (
    <article className={"macro-item" + (dim ? " dim" : "")}>
      <div className="macro-title">
        {item.url ? (
          <a href={item.url} target="_blank" rel="noreferrer">{item.title_zh ?? item.title}</a>
        ) : (item.title_zh ?? item.title)}
      </div>
      {(item.summary_zh ?? item.summary) ? (
        <p className="macro-sum">{item.summary_zh ?? item.summary}</p>
      ) : null}
      <div className="macro-meta">
        <span>{item.source_name ?? item.source_id}</span>
        {item.score_avg != null ? <span>score {item.score_avg.toFixed(1)}</span> : null}
        {item.project ? <span>{item.project}</span> : null}
        {item.sector ? <span>{item.sector}</span> : null}
        {item.morph ? <span>{item.morph}</span> : null}
        {item.published_at ? <span>{stamp(item.published_at)}</span> : null}
        {item.url ? (
          <a href={item.url} target="_blank" rel="noreferrer">{READ_ORIGINAL}</a>
        ) : null}
      </div>
      {item.reason ? <div className="macro-reason">{item.reason}</div> : null}
    </article>
  );
}

function ItemsView({ selected }: { selected: boolean }) {
  const feed = useMacroItems(selected, 50);
  if (feed.isPending) return <div className="note-bar">{LOADING}</div>;
  if (feed.isError) {
    return <div className="note-bar bad">{errorText(feed.error)}</div>;
  }
  const rows = feed.data ?? [];
  if (!rows.length) {
    return (
      <div className="note-bar">
        {selected
          ? zh(0x8fd8, 0x6ca1, 0x6709, 0x7cbe, 0x9009)
          : zh(0x8fd8, 0x6ca1, 0x6709, 0x52a8, 0x6001)}
      </div>
    );
  }
  return (
    <div className="macro-list">
      {rows.map((item) => (
        <ItemRow key={item.id} item={item} dim={selected ? false : !item.selected} />
      ))}
    </div>
  );
}

function EventRow({ event, kept, onToggle }:
    { event: MacroEvent; kept: boolean; onToggle: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const detail = useMacroEvent(open ? event.id : undefined);
  return (
    <article className="macro-item">
      <div className="macro-title" onClick={() => setOpen((v) => !v)} role="button">
        <Icon as={Flame} size={14} />
        {event.title}
      </div>
      <div className="macro-meta">
        <span>{zh(0x70ed, 0x5ea6)} {event.heat.toFixed(1)}</span>
        <span>{event.sources_count}{zh(0x5bb6, 0x6765, 0x6e90)}</span>
        <span>{event.items_count}{zh(0x6761, 0x76f8, 0x5173, 0x62a5, 0x9053)}</span>
        {event.is_rising ? <b>{zh(0x4e0a, 0x5347)}</b> : null}
        {event.is_new ? <b>{zh(0x65b0)}</b> : null}
        <span>{stamp(event.last_seen)}</span>
        <button type="button" className="btn sec" onClick={() => onToggle(event.id)}>
          <Icon as={Star} size={14} />
          {kept
            ? zh(0x53d6, 0x6d88, 0x6536, 0x85cf)
            : zh(0x6536, 0x85cf)}
        </button>
      </div>
      {open ? (
        detail.isPending ? <div className="note-bar">{LOADING}</div>
        : detail.isError ? <div className="note-bar bad">{errorText(detail.error)}</div>
        : (
          <div className="macro-list">
            {(detail.data?.items ?? []).map((item) => <ItemRow key={item.id} item={item} />)}
          </div>
        )
      ) : null}
    </article>
  );
}

function EventsView() {
  const board = useMacroEvents("heat");
  const shelf = useMacroFavorites();
  const keep = useKeepMacroFavorite();
  const forget = useForgetMacroFavorite();
  if (board.isPending) return <div className="note-bar">{LOADING}</div>;
  if (board.isError) return <div className="note-bar bad">{errorText(board.error)}</div>;
  const rows = board.data ?? [];
  if (!rows.length) {
    return <div className="note-bar">{zh(0x8fd8, 0x6ca1, 0x6709, 0x4e8b, 0x4ef6)}</div>;
  }
  const kept = new Set((shelf.data ?? []).map((row) => row.id));
  const toggle = (id: string) => {
    if (kept.has(id)) forget.mutate(id);
    else keep.mutate(id);
  };
  return (
    <div className="macro-list">
      {rows.map((event) => (
        <EventRow key={event.id} event={event} kept={kept.has(event.id)} onToggle={toggle} />
      ))}
    </div>
  );
}

function DigestView() {
  const digest = useMacroDigest();
  if (digest.isPending) return <div className="note-bar">{LOADING}</div>;
  if (digest.isError) {
    return (
      <div className="note-bar">
        {zh(0x6682, 0x65e0, 0x65e5, 0x62a5)}
      </div>
    );
  }
  const doc = digest.data;
  return (
    <div className="macro-list">
      {doc.intro ? <p className="macro-sum">{doc.intro}</p> : null}
      {doc.sections.map((sec) => (
        <section key={sec.sector}>
          <h4 className="look-h">{sec.sector}</h4>
          {sec.items.map((item) => <ItemRow key={item.id} item={item} />)}
        </section>
      ))}
    </div>
  );
}

const KIND_LABEL: Record<string, string> = {
  project: zh(0x9879, 0x76ee),
  sector: zh(0x8d5b, 0x9053),
  morph: zh(0x5f62, 0x6001),
};

function TopicsView({ onSearch }: { onSearch: (q: string) => void }) {
  const topics = useMacroTopics();
  if (topics.isPending) return <div className="note-bar">{LOADING}</div>;
  if (topics.isError) return <div className="note-bar bad">{errorText(topics.error)}</div>;
  const groups = new Map<string, typeof topics.data>();
  for (const row of topics.data ?? []) {
    const list = groups.get(row.kind) ?? [];
    list.push(row);
    groups.set(row.kind, list);
  }
  return (
    <div className="macro-list">
      {[...groups.entries()].map(([kind, rows]) => (
        <section key={kind}>
          <h4 className="look-h">{KIND_LABEL[kind] ?? kind}</h4>
          <div className="macro-chips">
            {rows?.map((row) => (
              <button key={kind + row.name} type="button" className="btn sec"
                onClick={() => onSearch(row.name)}>
                {row.name} · {row.count}
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function SearchView({ seed }: { seed: string }) {
  const [q, setQ] = useState(seed);
  const result = useMacroSearch(q);
  return (
    <div className="macro-list">
      <input className="fav-search" value={q} placeholder={zh(0x8f93, 0x5165, 0x5173, 0x952e, 0x8bcd)}
        onChange={(event) => setQ(event.target.value)} />
      {result.isPending && q.trim().length >= 2 ? <div className="note-bar">{LOADING}</div> : null}
      {result.isError ? <div className="note-bar bad">{errorText(result.error)}</div> : null}
      {(result.data ?? []).map((item) => <ItemRow key={item.id} item={item} />)}
    </div>
  );
}

/** The hotspot place: picked, feed, board, briefing, topics and news search. */
export function MacroView({ tab, onTab }: { tab: MacroTab; onTab: (next: MacroTab) => void }) {
  const collect = useTriggerMacroCollect();
  const [seed, setSeed] = useState("");
  const pickTopic = (name: string) => {
    setSeed(name);
    onTab("search");
  };
  return (
    <div id="view-macro">
      <div className="macro-head">
        <div className="tabs" role="tablist">
          {MACRO_TABS.map((entry) => (
            <button key={entry.id} type="button" role="tab" aria-selected={tab === entry.id}
              className={"tab" + (tab === entry.id ? " on" : "")}
              onClick={() => onTab(entry.id)}>
              {entry.label}
            </button>
          ))}
        </div>
        <button type="button" className="btn sec" disabled={collect.isPending}
          onClick={() => collect.mutate()}>
          {collect.isPending
            ? zh(0x91c7, 0x96c6, 0x4e2d) + "..."
            : zh(0x7acb, 0x5373, 0x91c7, 0x96c6)}
        </button>
      </div>
      {collect.isError ? (
        <div className="note-bar bad">{errorText(collect.error)}</div>
      ) : null}
      {tab === "picked" ? <ItemsView selected />
      : tab === "items" ? <ItemsView selected={false} />
      : tab === "events" ? <EventsView />
      : tab === "digest" ? <DigestView />
      : tab === "topics" ? <TopicsView onSearch={pickTopic} />
      : <SearchView key={seed} seed={seed} />}
    </div>
  );
}