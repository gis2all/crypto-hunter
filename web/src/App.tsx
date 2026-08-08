import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Batch, Card as CardModel } from "./api/client";
import { useBatch, useCancelRun, useChains, useFavorites, useForgetFavorite,
  useKeepFavorite, useLiveRun, useRuns } from "./api/hooks";
import { OverviewPanel } from "./features/overview/OverviewPanel";
import { FavoritesView } from "./features/favorites/FavoritesView";
import { LookupView } from "./features/lookup/LookupView";
import { OpportunityView } from "./features/opportunity/OpportunityView";
import { Rail, type Place } from "./features/rail/Rail";
import { ReportView } from "./features/report/ReportView";
import { batchToMarkdown, TopBar, type View } from "./features/report/TopBar";
import { RunPanel } from "./features/run/RunPanel";
import { ConfigManager } from "./features/settings/ConfigManager";
import { SettingsView } from "./features/settings/SettingsView";
import { useSettingsState } from "./features/settings/useSettingsState";
import { SystemView } from "./features/system/SystemView";
import { readPref, savePref } from "./prefs";
import { applyTheme, readTheme, saveTheme, type Theme } from "./theme";

//: A round in one of these has stopped moving, so the page stops watching it. `cancelled` is
//: one of them: a round the operator stopped is over, and a page that kept polling it would
//: keep the 已中止 panel open on a round that will never move again.
const TERMINAL = new Set(["researched", "cancelled", "failed"]);

/** Where the page keeps the reader's place. One key per thing remembered, all under the
 *  `ch.` prefix the theme already uses, so one search finds everything this app stores in
 *  a browser. See `prefs.ts`.
 *
 *  The set of settings being read, and the tab, are remembered **per radar**: they are that
 *  radar's view, and a reader who leaves Arc · 报告 for Robinhood · 设定 has not asked Arc to
 *  change tab. One shared key cannot do it - a config id is the backend's and unique to its
 *  chain, so a set read on one chain reads as "this set was just deleted" on the other and the
 *  correction below drops the reader onto the live set. That is how a set created and read
 *  before it was made live came back as 默认配置 (user, 2026-10-06). Which radar and which
 *  place stay single keys: the four places are not scoped by a chain. */
const CHAIN_KEY = "ch.chain";
const PLACE_KEY = "ch.place";
const configKey = (chainId: string) => `ch.config.${chainId}`;
const viewKey = (chainId: string) => `ch.view.${chainId}`;
/** What those two keys were before the memory went per radar. Read once as the fallback, so a
 *  reader in the middle of a session keeps the set they had open; nothing writes them now. */
const LEGACY_CONFIG_KEY = "ch.config";
const LEGACY_VIEW_KEY = "ch.view";

function download(batch: Batch) {
  const blob = new Blob([batchToMarkdown(batch)], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `crypto-hunter-${batch.batch_label.replace(/[^\d]+/g, "-").replace(/^-|-$/g, "")}.md`;
  link.click();
  URL.revokeObjectURL(url);
}

export default function App() {
  const queryClient = useQueryClient();
  const chains = useChains();
  // The page's own memory: which radar, which set of settings and which tab the reader was
  // on. A reload has to land where they left, not back on Arc · 报告. The round index is
  // deliberately not kept - see the note on `batchIndex` below.
  // The radar the page opens on, read once: the two remembered values below are keyed by it,
  // so they have to be read with the same id the states are created with.
  const [startChain] = useState(() => readPref(CHAIN_KEY) ?? "arc");
  const [chainId, setChainId] = useState(startChain);
  // Which set of settings the page is reading - this radar's last one, or nothing, which the
  // correction below turns into the live set once the list answers. Empty is not a choice: the
  // id is the backend's and the list is the only place it is written, so the page never
  // invents one. Everything below the chain list is scoped by this as well as by the chain.
  const [configId, setConfigId] = useState(
    () => readPref(configKey(startChain)) ?? readPref(LEGACY_CONFIG_KEY) ?? "",
  );
  const [configMgrOpen, setConfigMgrOpen] = useState(false);
  const [view, setView] = useState<View>(() => {
    const kept = readPref(viewKey(startChain)) ?? readPref(LEGACY_VIEW_KEY);
    return kept === "params" ? "params" : "report";
  });
  // 雷达, 收藏, 搜索, 新机会 or 系统设置. All five are places, not tabs: the last four are
  // scoped by neither the selected chain nor a set of settings, so none of them can live under
  // 报告 / 设定 - see Rail's Place. 新机会 belongs here for the same reason an activity does:
  // it belongs to no chain, so there is nothing for a chain selector to scope.
  const [place, setPlace] = useState<Place>(() => {
    const kept = readPref(PLACE_KEY);
    return kept === "favorites" || kept === "lookup" || kept === "system"
      || kept === "opportunity" ? kept : "radar";
  });
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [batchIndex, setBatchIndex] = useState(0);
  const [runPanelOpen, setRunPanelOpen] = useState(false);
  // 展开卡片 / 收起卡片 is one bulk edit over five uncontrolled <details>. Bumping the
  // tick remounts them with the new default, so the page never has to own each card's
  // open state to be able to open them all.
  const [bulk, setBulk] = useState({ open: false, tick: 0 });
  const [theme, setTheme] = useState<Theme>(readTheme);

  const chain = chains.data?.find((item) => item.id === chainId);
  const configs = useMemo(() => chain?.configs ?? [], [chain]);

  // The shelf, and the two writes a star performs. One query answers both readers: the 收藏
  // page draws the cards from it, and a report page only asks which of its CAs are on it.
  const shelf = useFavorites();
  const keep = useKeepFavorite();
  const forget = useForgetFavorite();
  // Scoped to the radar the report page is showing: a CA kept while reading another chain is
  // not a star on this page. The shelf itself is not scoped by anything.
  const favCas = useMemo(
    () => new Set((shelf.data ?? [])
      .filter((row) => row.chain_id === chainId)
      .map((row) => row.ca)),
    [shelf.data, chainId],
  );
  // One star, two outcomes. A card already on the shelf is taken off; one that is not is
  // kept from the round the page is showing, which is where its payload comes from.
  const onFavorite = (card: CardModel) => {
    if (!run) {
      return;
    }
    if (favCas.has(card.ca)) {
      forget.mutate({ chainId, ca: card.ca });
      return;
    }
    keep.mutate({ runId: run.id, ca: card.ca });
  };

  // Entering a radar is a save and a load, not a drag: the set being read and the tab belong to
  // the radar they were read on, so the outgoing one is written before the incoming one is read.
  // Nothing else moves the radar - the rail and the correction below both come through here.
  const openChain = (id: string) => {
    if (configId) {
      savePref(configKey(chainId), configId);
    }
    savePref(viewKey(chainId), view);
    setChainId(id);
    setConfigId(readPref(configKey(id)) ?? "");
    const kept = readPref(viewKey(id));
    setView(kept === "params" ? "params" : "report");
  };

  // A remembered radar that is no longer on the rail - dropped from the list, or turned off -
  // is not a place the page can be: the rail draws only the enabled chains, so the
  // page would sit on a chain with no row lit. It falls back to the first enabled chain, which
  // is where a first visit opens anyway, and it takes the same open as the rail so the radar
  // being left keeps what it was reading.
  useEffect(() => {
    const list = chains.data;
    if (!list?.length || list.some((item) => item.id === chainId && item.enabled)) {
      return;
    }
    const first = list.find((item) => item.enabled);
    if (first) {
      openChain(first.id);
    }
  }, [chains.data, chainId]);

  // Which set of settings the page opens on, answered per chain by the list. The reader's
  // own pick wins while it is still there - a poll, a rename or a reload must not move them
  // off it - and the chain's live set is the fallback: a set that was just deleted, or a
  // chain nobody has been in yet. Reading one set while another is live is allowed on
  // purpose, so this only ever corrects a scope that cannot be read at all.
  useEffect(() => {
    if (configs.length === 0 || configs.some((item) => item.id === configId)) {
      return;
    }
    setConfigId((configs.find((item) => item.enabled) ?? configs[0]).id);
  }, [configs, configId]);

  const runs = useRuns(chainId, configId);
  const list = useMemo(() => runs.data ?? [], [runs.data]);
  const run = list[batchIndex];
  const live = Boolean(run && !TERMINAL.has(run.status));
  const liveRun = useLiveRun(run?.id, live ? "running" : "off");
  const batch = useBatch(run?.id);
  // Held here, not in the page: the two buttons that act on it stand in the topbar.
  // A run just started is always the list head (the backend sorts newest first): point
  // the pager at it and open the panel at once. Without this the page keeps showing the
  // older round the reader had paged to, and the click looks dead until a reload
  // re-answers the pager.
  const settings = useSettingsState(chainId, configId, () => {
    setBatchIndex(0);
    setRunPanelOpen(true);
  });
  const cancel = useCancelRun(chainId, configId);

  // Which round the page opens on, answered once per chain by the first list that arrives.
  // A round the operator stopped - or one that died - has no cards in it, so the page opens
  // on the newest round that either published cards or has not stopped moving. Stepping back
  // to an older one is the pager's job, and that place lives only in this session: a reload
  // re-answers and lands on the newest readable round again.
  // After this first answer the number belongs to the pager: no poll, and no reload of the
  // list, moves a reader who has stepped off it.
  const opened = useRef<string | null>(null);
  useEffect(() => {
    // The answer is per set of settings, not per chain: each one keeps its own report
    // library, so each one opens on its own newest readable round.
    const scope = `${chainId}/${configId}`;
    if (list.length === 0 || !configId || opened.current === scope) {
      return;
    }
    opened.current = scope;
    const readable = list.findIndex(
      (item) => (item.selected ?? 0) > 0 || !TERMINAL.has(item.status),
    );
    setBatchIndex(readable > 0 ? readable : 0);
  }, [list, chainId, configId]);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Written on every change rather than on the way out: a tab closed by a crash keeps the
  // last place it was at, and there is no `beforeunload` handler to keep in step.
  useEffect(() => {
    savePref(CHAIN_KEY, chainId);
  }, [chainId]);

  // Each of the two is written under the radar it was read on. The pair and the chain move in
  // one commit - `openChain` sets all three together, after writing the outgoing pair itself -
  // so the chain here is the one the value belongs to, and this also seeds the per-radar key on
  // the first render after a reload, from whichever key answered for it (the legacy one, on the
  // way in).
  //
  // An empty id is the page before the chain list has answered, not a choice: storing it
  // would overwrite the remembered one with nothing on the first render of every reload.
  useEffect(() => {
    if (configId) {
      savePref(configKey(chainId), configId);
    }
  }, [configId, chainId]);

  useEffect(() => {
    savePref(viewKey(chainId), view);
  }, [view, chainId]);

  useEffect(() => {
    savePref(PLACE_KEY, place);
  }, [place]);

  useEffect(() => {
    if (live) {
      setRunPanelOpen(true);
    }
  }, [live]);

  // A round that finished keeps its place instead of closing itself. The step list is where
  // the round own timings are read - each row right column is that phase measured wall
  // clock - and folding the panel away the moment the last stage ended is how the answer to
  // "what took the time" was written and then hidden. A failed round, a stopped round and a
  // finished one all behave the same way now: the panel stays until 运行结果 folds it.

  // Closing the panel hands the page back, and a round the operator stopped has nothing to
  // hand it: it produced no cards. Sending the reader to the newest round that did publish is
  // where they were reading before they pressed 中止 - and the stopped round stays reachable
  // through the pager, one step newer, for anyone who wants to look at it.
  const closePanel = () => {
    setRunPanelOpen(false);
    if (run?.status !== "cancelled") {
      return;
    }
    const published = list.findIndex((item) => (item.selected ?? 0) > 0);
    if (published >= 0) {
      setBatchIndex(published);
    }
  };

  // The topbar switch is the only way to fold the panel, so the fold rule rides its
  // closing half. Opening it must never move the reader: they pressed it to read the round
  // they are already on.
  const toggleRunPanel = () => {
    if (runPanelOpen) {
      closePanel();
      return;
    }
    setRunPanelOpen(true);
  };

  useEffect(() => {
    const fresh = liveRun.data;
    if (fresh && run && fresh.status !== run.status) {
      void queryClient.invalidateQueries({ queryKey: ["runs", chainId, configId] });
      void queryClient.invalidateQueries({ queryKey: ["batch", run.id] });
    }
  }, [liveRun.data, run, chainId, configId, queryClient]);

  return (
    <div className="app">
      <Rail
        chains={chains.data}
        activeChainId={chainId}
        place={place}
        onSelectPlace={setPlace}
        onSelectChain={(id) => {
          openChain(id);
          setPlace("radar");
        }}
        theme={theme}
        onTheme={(next) => {
          saveTheme(next);
          setTheme(next);
        }}
      />
      <main className="main">
        {place === "favorites" ? (
          // The whole list, not just the names: the shelf's chain switcher is a fixed row
          // read off these, so it needs the order and which of them the rail draws.
          <FavoritesView chains={chains.data ?? []} />
        ) : place === "system" ? (
          // This machine's own wiring: no chain and no set of settings is in scope, which is
          // exactly why it is a place and not a third tab under a radar.
          <SystemView />
        ) : place === "lookup" ? (
          // The chain list is passed for the same kind of reading: a searched card can be on
          // a chain this app does not screen, and its foot still has to name the one it has
          // an id for.
          <LookupView chains={chains.data ?? []} />
        ) : place === "opportunity" ? (
          // 新机会: what the outside world is offering, found by this app's own scans. No chain
          // list and no set of settings is passed, because neither scopes an activity - the
          // page's own read carries everything it draws.
          <OpportunityView />
        ) : (
          <>
          <TopBar
            view={view}
            onView={setView}
            runPanelOpen={runPanelOpen}
            onToggleRunPanel={toggleRunPanel}
            hasRun={Boolean(run)}
            onOverview={() => setOverviewOpen((open) => !open)}
            onExport={() => batch.data && download(batch.data)}
            settings={settings}
            chain={chain}
            configId={configId}
            onSelectConfig={setConfigId}
            onManage={() => setConfigMgrOpen(true)}
            openAll={bulk.open}
            onToggleCards={() => setBulk((prev) => ({ open: !prev.open, tick: prev.tick + 1 }))}
          />
          {run ? (
            <RunPanel
              run={liveRun.data ?? run}
              running={live}
              open={runPanelOpen}
              batch={batch.data}
              onCancel={() => (run ? cancel.mutateAsync(run.id) : Promise.resolve())}
              cancelling={cancel.isPending}
            />
          ) : null}
          <div hidden={view !== "report"}>
            <ReportView
              batch={batch.data}
              loading={batch.isLoading || runs.isLoading}
              error={batch.error ?? runs.error}
              canPrev={batchIndex < list.length - 1}
              canNext={batchIndex > 0}
              onPrev={() => setBatchIndex((index) => Math.min(index + 1, list.length - 1))}
              onNext={() => setBatchIndex((index) => Math.max(index - 1, 0))}
              openAll={bulk.open}
              bulkTick={bulk.tick}
              favorited={favCas}
              onFavorite={onFavorite}
            />
          </div>
          <div hidden={view !== "params"}>
            <SettingsView state={settings} chain={chain} configId={configId} />
          </div>
          </>
        )}
      </main>
      <OverviewPanel batch={batch.data} open={overviewOpen} onClose={() => setOverviewOpen(false)} />
      <ConfigManager
        chain={chain}
        open={configMgrOpen}
        onClose={() => setConfigMgrOpen(false)}
        onSelect={setConfigId}
      />
    </div>
  );
}
