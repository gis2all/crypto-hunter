import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowDownAZ, ArrowDownWideNarrow, ArrowUpAZ, ArrowUpNarrowWide, ChevronDown, ChevronRight,
  ChevronUp, Clock, FoldVertical, Globe, Layers, ListChecks, LoaderCircle, Search, Settings2,
  TriangleAlert, UnfoldVertical,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { errorText } from "../../api/client";
import type { Chain, Favorite, Job, JobStage } from "../../api/client";
import { useCollectFavorite, useFavorites, useForgetFavorite, useLatestLookupJob,
  useLookupJob, useLookupRecent, useSearchCa } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { chainIdMark } from "../../components/chainMark";
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
 *  Every search keeps its card. The wall on the right is every address this page has read,
 *  one card each, and 重新搜索 on a card is the explicit way to pay for a fresh reading
 *  rather than the default - the card in hand is the one that was stored.
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

/** 链 / 排序, and the direction under the sort: the panel the shelf wears on the 收藏 page,
 *  standing in the column the wall stands beside (2026-10-11, user). The two groups are the same
 *  two lists for the same reason - a sort is two decisions, a key and a direction,
 *  and eight combinations read better as two short lists than as one dropdown nobody opens.
 *
 *  The chains are the ones the rail lists, always, whether or not the list holds one of them
 *  today: a control that comes and goes with the contents is one the reader has to go looking
 *  for, and "never searched this chain" is a real answer rather than a reason to hide the row.
 *  其他 is the one row that is not a chain: a CA settles its own chain, so the list can
 *  hold a token from a chain this app has no radar for, and no chain row can find it. */
type SortKey = "origin" | "name";
type SortDir = "asc" | "desc";

/** The first direction is the one a key opens on, so a key is never shown with its second
 *  choice highlighted: 生成时间 opens on 从新到旧, 名称 on A–Z.
 *
 *  生成时间 is the stamp a row's own card prints (origin_label) and the order the backend
 *  hands the list over in, so it is the default this page asks for nothing with. There is no
 *  分数 key: a searched card carries no 综合分, and a column of blanks is not a sort. */
const SORTS: {
  value: SortKey;
  label: string;
  icon: LucideIcon;
  dirs: [SortDir, string, LucideIcon][];
}[] = [
  { value: "origin", label: "生成时间", icon: Clock,
    dirs: [["desc", "从新到旧", ArrowDownWideNarrow], ["asc", "从旧到新", ArrowUpNarrowWide]] },
  { value: "name", label: "名称", icon: ArrowDownAZ,
    dirs: [["asc", "A–Z", ArrowDownAZ], ["desc", "Z–A", ArrowUpAZ]] },
];

/** The one row that is not a chain: every chain this app has no radar for. A sentinel rather
 *  than a chain id, so it cannot collide with a chain the backend would ever write - the same
 *  reason 收藏's own 搜索 row is one. */
const ELSEWHERE = "other";

/** The band under a searched card: which chain it is on and when this reading was taken.
 *
 *  Shorter than the shelf band on purpose. A search has no round and no set of settings, so
 *  the 配置 field drops out - and it stays out even when a round has since taken the row
 *  over and left a config_id on it (10.10): those are settings this lookup never ran under.
 *  And a card nobody collected has no 收藏时间 either - the row saved_at is when the address
 *  was first looked up, and printing it as a collection would be a date the reader never
 *  made. What this band prints instead is origin_label, the search's own clock, which is also
 *  the order this wall opens on. */
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
 *  switch in the bar above - the same control that folds a round. */
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
        ? shortCa(job.ca) + " · 跑完自动落库到搜索列表"
        : (job.row?.symbol ?? shortCa(job.ca)) + " · 已落库到搜索列表";

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
  const [error, setError] = useState("");
  /** The last search this page queued. 运行结果 folds that job step list, which is why a
   *  page that has never queued one of its own still folds the last search on record. */
  const [jobId, setJobId] = useState<string | null>(() => readPref(JOB_KEY) || null);
  const [panelOpen, setPanelOpen] = useState(false);
  /** 展开卡片 / 收起卡片 is one bulk edit over five uncontrolled <details>, the same control a
   *  radar bar carries: bumping the tick remounts them with the new default. */
  const [bulk, setBulk] = useState({ open: false, tick: 0 });
  /** 搜索管理, a drawer over the page - the same shape 配置管理 opens in. It edits the list
   *  the column below reads, which is why it is a drawer and not a row of controls. */
  const [mgrOpen, setMgrOpen] = useState(false);
  /** 链 / 排序 are view state, not kept anywhere: the list arrives newest first, which
   *  is exactly 生成时间 / 从新到旧, so the defaults below are the page asking for nothing. */
  const [chain, setChain] = useState("");
  const [key, setKey] = useState<SortKey>("origin");
  const [dir, setDir] = useState<SortDir>("desc");

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
  // The chains the rail lists, in that order, always. 其他 stands in for the ones this app has
  // no radar for, and appears only once the list actually holds one: until then it is a row
  // whose only possible answer is empty.
  const known = useMemo(() => chains.filter((c) => c.enabled).map((c) => c.id), [chains]);
  const elsewhere = useMemo(
    () => rows.some((row) => !known.includes(row.chain_id)),
    [rows, known],
  );
  const sortNow = SORTS.find((sort) => sort.value === key) ?? SORTS[0];
  /** What the wall draws: every search, filtered by chain and read in the chosen order. The
   *  records 搜索管理 edits stay on `rows`, because hiding a card here is not forgetting it
   * . */
  const listed = useMemo(() => {
    const list = rows.filter((row) => {
      if (chain === ELSEWHERE) {
        return !known.includes(row.chain_id);
      }
      return !chain || row.chain_id === chain;
    });
    const flip = dir === "asc" ? 1 : -1;
    // Two rows that compare equal keep one fixed order (CA), so the two directions are two
    // readings of the same list rather than two different shuffles of the equal ones.
    const tie = (a: Favorite, b: Favorite) =>
      a.ca.localeCompare(b.ca) || a.chain_id.localeCompare(b.chain_id);
    const name = (row: Favorite) => row.symbol ?? row.card.sym;
    return [...list].sort((a, b) => {
      if (key === "name") {
        return flip * name(a).localeCompare(name(b)) || tie(a, b);
      }
      return flip * a.origin_label.localeCompare(b.origin_label) || tie(a, b);
    });
  }, [rows, chain, key, dir, known]);

  // A search is over when the job says so: the card it wrote is now in the wall, and the panel
  // that was counting the stages has nothing left to say. One write per job, guarded by id,
  // because the poll keeps answering the same terminal reading until the page stops asking.
  const settled = job.data && job.data.status !== "running" ? job.data : null;
  // Seeded with the job this page was already holding, so a restored one counts as read:
  // the reader left that panel folded (or never opened it), and a reload that unfolds it is
  // the page moving under them - the same reason a round own panel starts folded.
  const answered = useRef<string | null>(jobId);
  useEffect(() => {
    if (!settled || answered.current === settled.id) {
      return;
    }
    answered.current = settled.id;
    void queryClient.invalidateQueries({ queryKey: ["lookup-recent"] });
    // The panel was opened to watch this search run, so a run that came back folds it away -
    // left open it would cover the card it was waiting for. One that failed keeps it open,
    // because the reason is written in there. The chain filter goes back to 全部 for the same
    // reason as the fold: a card settles its own chain, and a filter left on the last
    // one would hide the card that just landed.
    setPanelOpen(settled.status === "failed");
    setChain("");
  }, [settled, queryClient]);

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

  /** The star a card in the wall wears, drawn and written per row: it reads 书架 for whether to
   *  draw solid and writes the same (chain, CA) back, so one star and one shelf cannot disagree
   * . This used to be the selected card's star; the wall has no selection. */
  const toggle = (row: Favorite) => {
    if (kept.has(rowKey(row))) {
      forget.mutate({ chainId: row.chain_id, ca: row.ca });
      return;
    }
    keep.mutate({ chainId: row.chain_id, ca: row.ca });
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
              queued nothing of its own folds the last search on record. */}
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
          {/* The same bulk edit a radar bar makes. One label, two states, so the
              reader learns it once and it always says what the next click does. */}
          <button className="btn sec"
            onClick={() => setBulk((prev) => ({ open: !prev.open, tick: prev.tick + 1 }))}>
            <Icon as={bulk.open ? FoldVertical : UnfoldVertical} size={14} />
            {bulk.open ? "收起卡片" : "展开卡片"}
          </button>
          {/* 搜索管理: the records the wall is drawing. It stands between 展开卡片 and the box
              the way 配置管理 stands beside 保存并生效 on a radar - the same kind of
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

      {error ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>{error}</span>
        </div>
      ) : null}

      <div className="grid look-body">
        <aside className="fav-side">
          {/* 链 and 排序: the panel the 收藏 page's shelf wears, one group fewer -
              what they act on is the wall in the column beside them. */}
          <h4 className="look-h">链</h4>
          <div className="fav-opts">
            <button type="button" className={"fav-opt" + (chain === "" ? " on" : "")}
              onClick={() => setChain("")}>
              <Icon as={Layers} size={14} />
              全部
            </button>
            {known.map((id) => (
              <button type="button" key={id} className={"fav-opt" + (chain === id ? " on" : "")}
                onClick={() => setChain(id)}>
                {chainIdMark(id)}
                {chainName(labels, id)}
              </button>
            ))}
            {elsewhere ? (
              <button type="button" className={"fav-opt" + (chain === ELSEWHERE ? " on" : "")}
                onClick={() => setChain(ELSEWHERE)}>
                <Icon as={Globe} size={14} />
                其他
              </button>
            ) : null}
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
          {/* The direction reads as the sort's own second half rather than as a third key:
              it is fenced off and it says what the key above it means. */}
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

        <section className="look-card">
          {/* Every address this page has read, one card each: the wall the left panel
              filters and orders. It rides the radar's own grid, so a searched card
              and a round card are the same width whatever stands beside them.
              重新搜索 is the card's own control, drawn in its head rather than on a strip
              above it (Card.tsx). */}
          {recent.isError ? (
            <div className="note-bar bad">
              <Icon as={TriangleAlert} size={14} />
              <span>读取失败：{errorText(recent.error)}</span>
            </div>
          ) : listed.length ? (
            <div className="grid">
              {listed.map((row) => (
                <div className="look-cell" key={rowKey(row)}>
                  <Card
                    card={row.card}
                    openAll={bulk.open}
                    bulkTick={bulk.tick}
                    listed={false}
                    favorited={kept.has(rowKey(row))}
                    onFavorite={() => toggle(row)}
                    onRescan={() => run(row.ca)}
                    rescanBusy={busy}
                    foot={provenance(row, labels)}
                  />
                </div>
              ))}
            </div>
          ) : (
            <div className="note-bar">
              {busy ? (
                <>
                  <Icon as={LoaderCircle} size={14} className="spin" />
                  <span>正在搜索，运行结果面板里有每一步的进度。</span>
                </>
              ) : rows.length ? (
                "没有符合条件的搜索记录。"
              ) : (
                "还没有搜索过任何 CA。输入一个合约地址，它会留下一张卡。"
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
