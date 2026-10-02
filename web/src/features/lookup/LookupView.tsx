import { useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown, ChevronRight, ChevronUp, FoldVertical, ListChecks, LoaderCircle, Search,
  Settings2, TriangleAlert, UnfoldVertical,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { errorText } from "../../api/client";
import type { Chain, Favorite, Job, JobStage } from "../../api/client";
import { useCollectFavorite, useFavorites, useForgetFavorite, useLatestLookupJob,
  useLookupJob, useLookupRecent, useSearchCa } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { Logo } from "../../components/Logo";
import { readPref, savePref } from "../../prefs";
import { Card } from "../report/Card";
import { chainName, field, stamp } from "../report/foot";
import { Marker, mmss } from "../run/RunPanel";
import { LookupManager } from "./LookupManager";
import { rowKey, shortCa } from "./rows";

/** One address, on its own - the third place the page has (Rail, 搜索).
 *
 *  A round starts from a chain and sweeps it. This starts from a contract address, and the
 *  chain is the answer to the first question rather than the input: nothing about an address
 *  says where it lives, and the backend asks the feed before it asks anything else. A chain
 *  this app has no config for still gets a card - its on-chain half reads 未判定 while the
 *  market and off-chain halves answer - which is why the page needs no 简卡 and no
 *  supported-chain list in front of the box.
 *
 *  The search itself is a job and not a call: it is minutes of network and a model call, so
 *  the page queues it and watches it, and 运行结果 folds the five stages it went through -
 *  the same panel a round gets, because "what took the time" is the same question.
 *
 *  Two things make the second look at a token free. The search writes the card it just read
 *  as a record of looking the address up, so the list on the left is cards, not names; and
 *  clicking one **reads that stored card** rather than searching again. 重新搜索 is the
 *  explicit way to pay for a fresh one, which is why it is a button and not the default.
 *
 *  Nothing here is ranked. 综合分 is a percentile inside a round pool and a pool of one has
 *  no percentiles, so a searched card carries the facts and no score - handed to the same
 *  Card the report page draws, so the two cannot drift apart in shape. And with nothing to
 *  rank, there is nothing to turn either: one page, not the 搜索 / 设定 pair a radar wears -
 *  the second tab only ever explained the first.
 */

/** Where this page remembers the search it queued. The job outlives the component twice
 *  over: switching to 收藏 unmounts this page, and a reload rebuilds it - and a two-minute
 *  search turned into a blank panel by either is the panel not doing its job. The job row
 *  itself is on the server, so an id is all that has to survive. */
const JOB_KEY = "ch.lookjob";

/** Which card this page was last showing. The job id above is not enough on its own: a card
 *  opened off 最近搜索 belongs to no job. What has to survive is the row own (chain, CA), not a
 *  position in a list that refetches - a card held here would be the one copy on this page that
 *  never moves. */
const CARD_KEY = "ch.lookcard";

/** The band under a searched card: which chain it is on and when this reading was taken.
 *
 *  Shorter than the shelf band on purpose. A search has no round and no set of settings, so
 *  the 配置 field drops out - and it stays out even when a round has since taken the row
 *  over and left a config_id on it (10.10): those are settings this lookup never ran under.
 *  And a card nobody collected has no 收藏时间 either - the row saved_at is when the address
 *  was first looked up, and printing it as a collection would be a date the reader never
 *  made. What this band prints instead is origin_label, the search's own clock, which is also
 *  what 最近搜索 is ordered by. */
function provenance(row: Favorite, labels: Map<string, string>) {
  return (
    <>
      <span>{field("链", chainName(labels, row.chain_id))}</span>
      <span>{field("生成时间", stamp(row.origin_label))}</span>
    </>
  );
}

/** The search, as a step list.
 *
 *  Drawn with the round panel own stylesheet (proto.css rp-* / rst) and the round own marker
 *  and duration format, so 认链 here and 拉取扫描源 there are the same kind of row. Two
 *  differences: the bar is the share of stages that have ENDED rather than a percent the
 *  pipeline reported, because a search has no finer checkpoint to read; and there is no 中止,
 *  because the backend has nothing that could stop a queued search. Folding it is the 运行结果
 *  switch in the bar above - the same control that folds a round (8.3.7). */
function SearchPanel({ job, open, labels, queued }: {
  job: Job | undefined;
  open: boolean;
  labels: Map<string, string>;
  /** True while this page's own job has been queued but has not answered yet. A page with no
   *  job at all reads differently: 运行结果 always has something to fold (see panelJob), and
   *  claiming a queue that does not exist is the one thing this panel must not do. */
  queued: boolean;
}) {
  const stages = job?.stages ?? [];
  const ended = stages.filter((stage) => stage.state === "done").length;
  const progress = stages.length ? ended / stages.length : 0;
  const failed = job?.status === "failed";
  const waiting = !job && queued;
  const running = waiting || job?.status === "running";
  const seconds = stages.reduce((total, stage) => total + (stage.seconds ?? 0), 0);
  const current = stages.find((stage) => stage.state === "run");

  // The one reading a stage can carry here: the chain 认链 settled on, and the symbol the
  // card went out with. The rest are the sources' own quiet work and stay blank rather than
  // borrowing a neighbour number.
  const result = (stage: JobStage): string => {
    if (stage.key === "resolve") {
      return job?.chain_id ? "→ " + chainName(labels, job.chain_id) : "—";
    }
    if (stage.key === "card") {
      return job?.row?.symbol ? "→ " + job.row.symbol : "—";
    }
    return "—";
  };

  const foot = !job
    ? (queued
      ? "正在排队 · 这一个 CA"
      : "还没有搜索过 —— 上面搜一次，每一步都会记在这里")
    : failed
      ? shortCa(job.ca) + " · 搜索失败：" + (job.error ?? "未记录原因")
      : running
        ? shortCa(job.ca) + " · 跑完自动落库到「最近搜索」"
        : (job.row?.symbol ?? shortCa(job.ca)) + " · 已落库到「最近搜索」";

  return (
    <div className="runpanel" id="lookpanel" hidden={!open}>
      <div className="rp-h">
        <b id="lp-title">
          {!job ? (queued ? "正在排队 · 这个 CA" : "还没有搜索记录")
            : failed ? "搜索失败 · 这个 CA"
              : running ? "正在搜索 · 这个 CA" : "搜索完成 · 这个 CA"}
        </b>
        {job ? (
          <span className="rp-el" id="lp-el">
            {running ? "已用 " : "用时 "}{mmss(Math.round(seconds))}
          </span>
        ) : null}
        {/* No percent on a failure: the stage that ended is the one it died on, so the
            share of finished stages only ever reads as a number that stopped. The failed
            row says the same thing in words. */}
        {job && stages.length && !failed ? (
          <span className="rp-el" id="lp-pct">
            {Math.round(progress * 100)}%{current ? " · " + current.label : ""}
          </span>
        ) : null}
        <span className="spacer" />
      </div>
      <div className="rp-bar">
        <i id="lp-fill" style={{ transform: "scaleX(" + progress.toFixed(4) + ")" }} />
      </div>
      <ol className="rp-steps" id="lp-steps">
        {stages.map((stage) => (
          <li className={"rst " + stage.state} key={stage.key}>
            <Marker state={stage.state as "pending" | "run" | "done" | "failed"} />
            <b>{stage.label}</b>
            <span className="rst-r">{result(stage)}</span>
            <em className="rst-t">
              {stage.seconds === null || stage.seconds === undefined
                ? "—"
                : mmss(Math.round(stage.seconds))}
            </em>
          </li>
        ))}
      </ol>
      <div className="rp-foot" id="lp-foot">{foot}</div>
    </div>
  );
}

export function LookupView({ chains }: { chains: Chain[] }) {
  const queryClient = useQueryClient();
  const recent = useLookupRecent();
  const shelf = useFavorites();
  const keep = useCollectFavorite();
  const forget = useForgetFavorite();
  const search = useSearchCa();

  const [ca, setCa] = useState("");
  /** The row the reader clicked in this sitting, and only that. Which card is on screen is
   *  derived below, because the other two things that decide it - the last search, and the row
   *  this page was left on - are not clicks. Opening one is a state change and not a request,
   *  which is what makes it instant. */
  const [picked, setPicked] = useState<Favorite | null>(null);
  /** The card this page was showing when the reader last left it, as a (chain, CA) key. */
  const [held, setHeld] = useState<string | null>(() => readPref(CARD_KEY) || null);
  const [error, setError] = useState("");
  /** The last search this page queued. 运行结果 folds that job step list, which is why a
   *  page that has never queued one of its own still folds the last search on record. */
  const [jobId, setJobId] = useState<string | null>(() => readPref(JOB_KEY) || null);
  const [panelOpen, setPanelOpen] = useState(false);
  /** 展开卡片 / 收起卡片 is one bulk edit over five uncontrolled <details>, the same control a
   *  radar bar carries (8.3.7): bumping the tick remounts them with the new default. */
  const [bulk, setBulk] = useState({ open: false, tick: 0 });
  /** 搜索管理, a drawer over the page - the same shape 配置管理 opens in. It edits the list
   *  the column below reads, which is why it is a drawer and not a row of controls. */
  const [mgrOpen, setMgrOpen] = useState(false);

  const job = useLookupJob(jobId);
  // The fallback reading. A reloaded page still holds its job id in ch.lookjob, so this is for
  // the reader who opens 搜索 fresh: 运行结果 must fold the last search, not read empty.
  const latest = useLatestLookupJob();
  const panelJob = job.data ?? latest.data ?? undefined;
  // Queued and unanswered, which is not the same thing as nothing on record.
  const queued = Boolean(jobId) && !job.data;
  const rows = useMemo(() => recent.data ?? [], [recent.data]);
  // A searched card keeps its own chain id even when this app has no radar for that chain, so
  // the names are read off every chain rather than off the rail enabled ones.
  const labels = useMemo(() => new Map(chains.map((c) => [c.id, c.name])), [chains]);
  // Which searched cards the reader has collected. The star is drawn off the shelf rather than
  // off this list, so one star and one shelf can never disagree.
  const kept = useMemo(() => new Set((shelf.data ?? []).map((row) => rowKey(row))), [shelf.data]);

  // A search is over when the job says so, and the page answers both ways at once: the card
  // the search wrote is now missing from the list it belongs to, and the reader is owed the
  // card itself. One write per job, guarded by id, because the poll keeps answering the same
  // terminal reading until the page stops asking.
  const settled = job.data && job.data.status !== "running" ? job.data : null;
  const landed = settled?.row ?? null;
  /** Which card the column is showing, in the order the reader would name them: the row clicked
   *  just now, else the row this page was left on, else the card the last search wrote. Derived
   *  rather than held, because neither of the last two is a click - this view unmounts with its
   *  Place, and a page that remembers the search while dropping the card hands back an empty
   *  column and a hint. */
  const restored = useMemo(
    () => (held ? rows.find((row) => rowKey(row) === held) ?? null : null),
    [held, rows],
  );
  const shown = picked ?? restored ?? landed ?? null;
  /** Put a row on screen and write it down, so the next visit starts where this one ended. */
  const remember = (row: Favorite) => {
    setPicked(row);
    setHeld(rowKey(row));
    savePref(CARD_KEY, rowKey(row));
  };
  // Seeded with the job this page was already holding, so a restored one counts as read:
  // the reader left that panel folded (or never opened it), and a reload that unfolds it is
  // the page moving under them - the same reason a round own panel starts folded (8.3.7).
  const answered = useRef<string | null>(jobId);
  useEffect(() => {
    if (!settled || answered.current === settled.id) {
      return;
    }
    answered.current = settled.id;
    void queryClient.invalidateQueries({ queryKey: ["lookup-recent"] });
    setPanelOpen(true);
    if (landed) {
      remember(landed);
    }
  }, [settled, landed, queryClient]);

  const live = Boolean(jobId) && !settled;
  const busy = search.isPending || live;

  // A restored id the backend no longer has is dropped rather than kept: the poll has
  // already stopped on its own (a 404 cannot become a job), and the switch that folds a
  // job nobody can read is worse than the switch knowing there is none.
  useEffect(() => {
    if (job.isError) {
      savePref(JOB_KEY, "");
      setJobId(null);
    }
  }, [job.isError]);

  const run = (address: string) => {
    const value = address.trim();
    if (!value || busy) {
      return;
    }
    setError("");
    search.mutate(value, {
      onSuccess: (fresh) => {
        savePref(JOB_KEY, fresh.id);
        setJobId(fresh.id);
        setPanelOpen(true);
      },
      onError: (err) => setError(errorText(err)),
    });
  };

  const starred = shown ? kept.has(rowKey(shown)) : false;
  const onStar = () => {
    if (!shown) {
      return;
    }
    if (starred) {
      forget.mutate({ chainId: shown.chain_id, ca: shown.ca });
      return;
    }
    keep.mutate({ chainId: shown.chain_id, ca: shown.ca });
  };

  return (
    <div id="view-lookup">
      <div className="fav-bar">
        {/* A title, not the 报告 / 设定 pair a radar wears: a search has no pool to size rules
            against, so the second tab only ever repeated the first. */}
        <div className="fav-title">
          <Icon as={Search} size={14} />
          <b>搜索</b>
        </div>
        <div className="fav-tools">
          {/* The leftmost control in the actions row, on purpose: it belongs to the search the
              way 默认配置 and 展开卡片 belong to a radar. Never disabled - a page that has
              queued nothing of its own folds the last search on record (8.8.11). */}
          <button
            className="btn sec"
            id="look-toggle"
            aria-expanded={panelOpen}
            title={panelOpen ? "收起运行结果" : "展开运行结果"}
            onClick={() => setPanelOpen((open) => !open)}
          >
            <Icon as={ListChecks} size={14} />
            运行结果
            <Icon as={panelOpen ? ChevronUp : ChevronDown} size={14} />
          </button>
          {/* The same bulk edit a radar bar makes (8.3.7). One label, two states, so the
              reader learns it once and it always says what the next click does. */}
          <button className="btn sec"
            onClick={() => setBulk((prev) => ({ open: !prev.open, tick: prev.tick + 1 }))}>
            <Icon as={bulk.open ? FoldVertical : UnfoldVertical} size={14} />
            {bulk.open ? "收起卡片" : "展开卡片"}
          </button>
          {/* 搜索管理: the records 最近搜索 is holding. It stands between 展开卡片 and the box
              the way 配置管理 stands beside 保存并生效 on a radar (8.3.7) - the same kind of
              control, so it wears the same box and the same trailing chevron. */}
          <button className="btn sec" onClick={() => setMgrOpen(true)}>
            <Icon as={Settings2} size={14} />
            搜索管理
            <Icon as={ChevronRight} size={14} />
          </button>
          <input
            className="fav-search"
            value={ca}
            placeholder="合约地址（任意链）"
            spellCheck={false}
            onChange={(event) => setCa(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                run(ca);
              }
            }}
          />
          <button type="button" className="btn" disabled={busy} onClick={() => run(ca)}>
            <Icon as={Search} size={14} />
            {busy ? "搜索中…" : "搜索"}
          </button>
        </div>
      </div>

      <SearchPanel job={panelJob} open={panelOpen} labels={labels} queued={queued} />
      <LookupManager rows={rows} kept={kept} labels={labels}
        open={mgrOpen} onClose={() => setMgrOpen(false)} />

      <>
          {error ? (
            <div className="note-bar bad">
              <Icon as={TriangleAlert} size={14} />
              <span>{error}</span>
            </div>
          ) : null}

          <div className="grid look-body">
            <aside className="look-list">
              <h4 className="look-h">最近搜索</h4>
              {recent.isError ? (
                <div className="note-bar bad">
                  <Icon as={TriangleAlert} size={14} />
                  <span>读取失败：{errorText(recent.error)}</span>
                </div>
              ) : rows.length ? rows.map((row) => (
                <button
                  type="button"
                  key={rowKey(row)}
                  className={"look-row" + (shown && rowKey(shown) === rowKey(row) ? " on" : "")}
                  onClick={() => remember(row)}
                >
                  <span className="look-top">
                    <Logo url={row.card.logo} size={19} />
                    <b>{row.symbol ?? "—"}</b>
                    <i>{chainName(labels, row.chain_id)}</i>
                  </span>
                </button>
              )) : (
                <div className="note-bar">还没有搜索过任何 CA。</div>
              )}
            </aside>

            <section className="look-card">
              {shown ? (
                // The card rides the radar own grid, so a searched card and a round card are
                // the same width whatever stands beside them. 重新搜索 is the card's own
                // control now, drawn in its head instead of on a strip above it (Card.tsx).
                <div className="grid">
                  <div className="look-cell">
                    <Card
                      card={shown.card}
                      openAll={bulk.open}
                      bulkTick={bulk.tick}
                      listed={false}
                      favorited={starred}
                      onFavorite={onStar}
                      onRescan={() => run(shown.ca)}
                      rescanBusy={busy}
                      foot={provenance(shown, labels)}
                    />
                  </div>
                </div>
              ) : (
                <div className="note-bar">
                  {busy ? (
                    <>
                      <Icon as={LoaderCircle} size={14} className="spin" />
                      <span>正在搜索，运行结果面板里有每一步的进度。</span>
                    </>
                  ) : (
                    "输入一个合约地址，或者从左边的最近搜索里点一个 —— 点开的是当初存下的那张卡，不用再等一次搜索。"
                  )}
                </div>
              )}
            </section>
          </div>
      </>
    </div>
  );
}
