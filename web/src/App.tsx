import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Batch } from "./api/client";
import { useBatch, useCancelRun, useChains, useLiveRun, useRuns } from "./api/hooks";
import { OverviewPanel } from "./features/overview/OverviewPanel";
import { Rail } from "./features/rail/Rail";
import { ReportView } from "./features/report/ReportView";
import { batchToMarkdown, TopBar, type View } from "./features/report/TopBar";
import { RunPanel } from "./features/run/RunPanel";
import { SettingsView } from "./features/settings/SettingsView";
import { useSettingsState } from "./features/settings/useSettingsState";
import { applyTheme, readTheme, saveTheme, type Theme } from "./theme";

//: A round in one of these has stopped moving, so the page stops watching it. `cancelled` is
//: one of them: a round the operator stopped is over, and a page that kept polling it would
//: keep the 已中止 panel open on a round that will never move again.
const TERMINAL = new Set(["sent", "researched", "cancelled", "failed"]);

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
  const [chainId, setChainId] = useState("arc");
  const [view, setView] = useState<View>("report");
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [batchIndex, setBatchIndex] = useState(0);
  const [runPanelOpen, setRunPanelOpen] = useState(false);
  // 全部展开 / 全部收起 is one bulk edit over five uncontrolled <details>. Bumping the
  // tick remounts them with the new default, so the page never has to own each card's
  // open state to be able to open them all.
  const [bulk, setBulk] = useState({ open: false, tick: 0 });
  const [theme, setTheme] = useState<Theme>(readTheme);

  const runs = useRuns(chainId);
  const list = useMemo(() => runs.data ?? [], [runs.data]);
  const run = list[batchIndex];
  const live = Boolean(run && !TERMINAL.has(run.status));
  // `sent` is not the end of the page: the cards are up and the research pass is still
  // reading the sites they were published from. Polling here is what lets the upgraded
  // words appear without asking the reader to reload.
  const settling = run?.status === "sent";
  const liveRun = useLiveRun(run?.id, live ? "running" : settling ? "settling" : "off");
  const batch = useBatch(run?.id);
  // Held here, not in the page: the two buttons that act on it stand in the topbar.
  const settings = useSettingsState(chainId);
  const cancel = useCancelRun(chainId);

  // Which round the page opens on, answered once per chain by the first list that arrives.
  // A round the operator stopped - or one that died - has no cards in it, so the page opens
  // on the newest round that either published cards or has not stopped moving. 收起 walks
  // back to that round by hand, and a reload has to land where 收起 left the reader.
  // After this first answer the number belongs to the pager: no poll, and no reload of the
  // list, moves a reader who has stepped off it.
  const opened = useRef<string | null>(null);
  useEffect(() => {
    if (list.length === 0 || opened.current === chainId) {
      return;
    }
    opened.current = chainId;
    const readable = list.findIndex(
      (item) => (item.selected ?? 0) > 0 || !TERMINAL.has(item.status),
    );
    setBatchIndex(readable > 0 ? readable : 0);
  }, [list, chainId]);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    if (live) {
      setRunPanelOpen(true);
    }
  }, [live]);

  // A round that finished has nothing left to watch, so the panel closes itself and hands
  // the strip back to the cards. A round that failed keeps its place: the step it died on
  // is the whole message, and 收起 is still there for when the reader is done with it. A
  // round the operator stopped keeps its place for the same reason - 已中止 is the answer to
  // the button they just pressed - and it is one line tall, so it costs nothing to leave.
  useEffect(() => {
    if (run && !live && run.status !== "failed" && run.status !== "cancelled") {
      setRunPanelOpen(false);
    }
  }, [run, live]);

  useEffect(() => {
    const fresh = liveRun.data;
    if (fresh && run && fresh.status !== run.status) {
      void queryClient.invalidateQueries({ queryKey: ["runs", chainId] });
      void queryClient.invalidateQueries({ queryKey: ["batch", run.id] });
    }
  }, [liveRun.data, run, chainId, queryClient]);

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
          <SettingsView state={settings} />
        </div>
      </main>
      <OverviewPanel batch={batch.data} open={overviewOpen} onClose={() => setOverviewOpen(false)} />
    </div>
  );
}
