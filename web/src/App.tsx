import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Batch } from "./api/client";
import { useBatch, useCancelRun, useChains, useLiveRun, useRuns } from "./api/hooks";
import { OverviewPanel } from "./features/overview/OverviewPanel";
import { Rail } from "./features/rail/Rail";
import { ReportView } from "./features/report/ReportView";
import { batchToMarkdown, TopBar, type View } from "./features/report/TopBar";
import { RunPanel } from "./features/run/RunPanel";
import { ConfigManager } from "./features/settings/ConfigManager";
import { SettingsView } from "./features/settings/SettingsView";
import { useSettingsState } from "./features/settings/useSettingsState";
import { readPref, savePref } from "./prefs";
import { applyTheme, readTheme, saveTheme, type Theme } from "./theme";

//: A round in one of these has stopped moving, so the page stops watching it. `cancelled` is
//: one of them: a round the operator stopped is over, and a page that kept polling it would
//: keep the 已中止 panel open on a round that will never move again.
const TERMINAL = new Set(["researched", "cancelled", "failed"]);

/** Where the page keeps the reader's place. One key per thing remembered, all under the
 *  `ch.` prefix the theme already uses, so one search finds everything this app stores in
 *  a browser. See `prefs.ts`. */
const CHAIN_KEY = "ch.chain";
const CONFIG_KEY = "ch.config";
const VIEW_KEY = "ch.view";

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
  const [chainId, setChainId] = useState(() => readPref(CHAIN_KEY) ?? "arc");
  // Which set of settings the page is reading. Empty until the chain list answers: the id
  // is the backend's and the list is the only place it is written, so the page never
  // invents one. Everything below the chain list is scoped by this as well as by the chain.
  const [configId, setConfigId] = useState(() => readPref(CONFIG_KEY) ?? "");
  const [configMgrOpen, setConfigMgrOpen] = useState(false);
  const [view, setView] = useState<View>(() => (readPref(VIEW_KEY) === "params" ? "params" : "report"));
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

  // A remembered radar that is no longer on the rail - dropped from the list, or turned off -
  // is not a place the page can be: the rail draws only the enabled chains (8.3.1), so the
  // page would sit on a chain with no row lit. It falls back to the first enabled chain, which
  // is where a first visit opens anyway.
  useEffect(() => {
    const list = chains.data;
    if (!list?.length || list.some((item) => item.id === chainId && item.enabled)) {
      return;
    }
    const first = list.find((item) => item.enabled);
    if (first) {
      setChainId(first.id);
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
  const settings = useSettingsState(chainId, configId);
  const cancel = useCancelRun(chainId, configId);

  // Which round the page opens on, answered once per chain by the first list that arrives.
  // A round the operator stopped - or one that died - has no cards in it, so the page opens
  // on the newest round that either published cards or has not stopped moving. 收起 walks
  // back to that round by hand, and a reload has to land where 收起 left the reader.
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

  // An empty id is the page before the chain list has answered, not a choice: storing it
  // would overwrite the remembered one with nothing on the first render of every reload.
  useEffect(() => {
    if (configId) {
      savePref(CONFIG_KEY, configId);
    }
  }, [configId]);

  useEffect(() => {
    savePref(VIEW_KEY, view);
  }, [view]);

  useEffect(() => {
    if (live) {
      setRunPanelOpen(true);
    }
  }, [live]);

  // A round that finished keeps its place instead of closing itself. The step list is where
  // the round own timings are read - each row right column is that phase measured wall
  // clock - and folding the panel away the moment the last stage ended is how the answer to
  // "what took the time" was written and then hidden. A failed round, a stopped round and a
  // finished one all behave the same way now: the panel stays until 收起 is pressed.

  useEffect(() => {
    const fresh = liveRun.data;
    if (fresh && run && fresh.status !== run.status) {
      void queryClient.invalidateQueries({ queryKey: ["runs", chainId, configId] });
      void queryClient.invalidateQueries({ queryKey: ["batch", run.id] });
    }
  }, [liveRun.data, run, chainId, configId, queryClient]);

  // 收起 hands the page back, and a round the operator stopped has nothing to hand it: it
  // produced no cards. Sending the reader to the newest round that did publish is where they
  // were reading before they pressed 中止 - and the stopped round stays reachable through the
  // pager, one step newer, for anyone who wants to look at it.
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

  return (
    <div className="app">
      <Rail
        chains={chains.data}
        activeChainId={chainId}
        onSelectChain={setChainId}
        theme={theme}
        onTheme={(next) => {
          saveTheme(next);
          setTheme(next);
        }}
      />
      <main className="main">
        <TopBar
          view={view}
          onView={setView}
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
            onCancel={() => run && cancel.mutate(run.id)}
            cancelling={cancel.isPending}
            onClose={closePanel}
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
          />
        </div>
        <div hidden={view !== "params"}>
          <SettingsView state={settings} chain={chain} configId={configId} />
        </div>
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
