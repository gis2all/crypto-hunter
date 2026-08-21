/** 新机会: what the outside world is offering, as this app's own scans found it.
 *
 *  A Place, not a tab under a radar: an activity belongs to no chain and no set of settings,
 *  so there is nothing here for a chain selector to scope.
 *
 *  One list, one panel. The entries were two lists plus two folds; they are one list filtered
 *  by 状态 now, because "未晋升" and "已结束" are the same rows seen from another side, and
 *  showing the same row in two places is how a page starts disagreeing with itself.
 *
 *  The panel is the shelf's own (`fav-side` in the first grid track, `.fav-opts` rows, the
 *  direction row under the sort) and it is built by the backend: which groups exist, what each
 *  option is called, and which sort keys are offered all arrive in the read, and this file
 *  recognises none of them. Filtering and sorting happen here because the read is the whole
 *  list - the same division of labour the 收藏 and 搜索 pages already use.
 */

import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ArrowDownWideNarrow,
  ArrowUpNarrowWide,
  Ban,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  Clock,
  ExternalLink,
  FileText,
  Inbox,
  FoldVertical,
  ListChecks,
  LoaderCircle,
  Play,
  Save,
  SlidersHorizontal,
  Trash2,
  TriangleAlert,
  UnfoldVertical,
} from "lucide-react";

import {
  errorText,
  type OpportunityFilter,
  type OpportunityItem,
  type OpportunityScan,
} from "../../api/client";
import {
  useCancelOpportunityScan,
  useOpportunity,
  useOpportunityQueues,
  useOpportunityScans,

  useOpportunitySources,
  useSetOpportunityState,
  useStartOpportunityScan,
} from "../../api/hooks";
import { OpportunitySettings } from "./OpportunitySettings";
import { useOpportunitySettingsState } from "./useOpportunitySettingsState";
import { Icon } from "../../components/Icon";
import { readPref, savePref } from "../../prefs";
import { Marker, mmss } from "../run/RunPanel";

const VIEW_KEY = "ch.opportunity.view";

/** The page's own copy, as the backend hands it over. Every sentence the page prints is here
 *  rather than in this file: the frontend does not recognise any of them (CLAUDE.md · 设计取舍). */
type OpportunityForm = {
  title?: string;
  trust?: Record<string, { label: string; tone: string }>;
  empty?: string;
  sources_title?: string;
  sources_lead?: string;
};

/** 排序键的图标，和 收藏 / 搜索 两页一样由前端挑：后端给的是键与名字，图标是装饰。
 *  方向那一对也与它们同一套（`ArrowDownWideNarrow` / `ArrowUpNarrowWide`），不是上下箭头。 */
const SORT_ICONS: Record<string, typeof Clock> = { discovered: Clock, deadline: CalendarClock };
const DIR_ICONS: Record<string, typeof Clock> = {
  desc: ArrowDownWideNarrow,
  asc: ArrowUpNarrowWide,
};

/** 一个筛选项读条目上的哪个字段。多一个分组时只改这里与后端，不动布局。 */
function matches(item: OpportunityItem, key: string, value: string): boolean {
  if (!value) {
    return true;
  }
  switch (key) {
    case "status":
      return item.status === value;
    case "kind":
      return item.kind === value;
    case "trust":
      return item.trust === value;
    case "category":
      return item.category === value;
    case "heat_band":
      return item.heat_band === value;
    case "ecosystem":
      return item.ecosystem === value;
    case "source_type":
      return item.source_type === value;
    case "barrier":
      // 门槛是多选字段，这里一次只筛一档：`.fav-opts` 是单选的行。
      return item.barrier.includes(value);
    default:
      return true;
  }
}

/** One stored timestamp, cut to the minute: the seconds never matter and the string is long. */
function stamp(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return value.slice(0, 16).replace("T", " ");
}

/** A stage under half a second is real work, not zero: show it as <1s instead of 0:00. */
function stageTime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "—";
  }
  if (seconds > 0 && seconds < 0.5) {
    return "<1s";
  }
  return mmss(Math.round(seconds));
}

/** 热度的一档，读成 pill 的颜色。未判定与静默都压暗：一个是没问到，一个是没人看，
 *  两者都不该抢眼。 */
function heatTone(band: string): string {
  if (band === "极热" || band === "热") {
    return "warn";
  }
  if (band === "温" || band === "冷启动") {
    return "";
  }
  return "muted";
}

/** The app's own tone words, as the modifier `.pill` already takes (`pill-ok` / `pill-warn` /
 *  `pill-danger` / `pill-muted`; a bare `.pill` is the neutral one). Composed here rather than
 *  inline because the first cut wrote `pill warn`, which is no class at all - the badges came
 *  out colourless and read as one more piece of grey text. */
function pillClass(tone: string): string {
  return tone ? `pill pill-${tone}` : "pill";
}

/** The scan, as a step list. Drawn with the round panel's own stylesheet and marker, so
 *  抓材料 here and 拉取扫描源 there are the same kind of row. */
function ScanPanel({ scan, open, form }: {
  scan: OpportunityScan | null | undefined;
  open: boolean;
  form: OpportunityForm;
}) {
  const stages = scan?.stages ?? [];
  const ended = stages.filter((stage) => stage.state === "done").length;
  const progress = stages.length ? ended / stages.length : 0;
  const failed = scan?.status === "failed";
  const running = scan?.status === "running";
  const counters = (scan?.counters ?? {}) as Record<string, unknown>;
  const collect = (counters.collect ?? {}) as Record<string, number>;
  const classify = (counters.classify ?? {}) as Record<string, number>;
  const extract = (counters.extract ?? {}) as Record<string, number>;
  const seconds = stages.reduce((total, stage) => total + (stage.seconds ?? 0), 0);
  const current = stages.find((stage) => stage.state === "run");

  // What each stage found, in its own units. A stage with nothing to report keeps the dash
  // rather than borrowing a neighbour's number.
  const result = (key: string): string => {
    if (key === "sources") {
      return counters.watchlist
        ? `账号 ${counters.accounts ?? 0} · 查询 ${counters.queries ?? 0}`
        : "—";
    }
    if (key === "collect") {
      // 这一格报的是 X 调用次数与过闸条数：查询行一条会变成好几次调用（切片），而
      // 预算花的是调用不是行。
      return collect.posts === undefined
        ? "—"
        : `帖子 ${collect.posts} · 过闸 ${collect.gated ?? 0} · 排除 ${collect.excluded ?? 0}`
          + ` · X 调用 ${collect.queries ?? 0}`;
    }
    if (key === "classify") {
      if (classify.groups !== undefined) {
        return `分组 ${classify.groups} · 无关 ${classify.irrelevant ?? 0}`
          + ` · 待定 ${classify.uncertain ?? 0}`;
      }
      return classify.asked === undefined ? "—" : `问了 ${classify.asked} 条`;
    }
    if (key === "extract") {
      return extract.entries === undefined
        ? "—"
        : `条目 ${extract.entries} · 晋升 ${extract.promoted ?? 0}`;
    }
    if (key === "sweep") {
      const sweep = (counters.sweep ?? {}) as Record<string, number>;
      return sweep.retired === undefined
        ? "—"
        : `退休 ${sweep.retired} · 重算热度 ${sweep.heat_refreshed ?? 0}`;
    }
    return "—";
  };

  const note = typeof counters.note === "string" ? counters.note : "";
  const foot = !scan
    ? String(form.empty ?? "还没有扫描过。")
    : failed
      ? "扫描失败：" + (scan.error ?? "未记录原因")
      : running
        ? "正在扫描 · 跑完自动落库"
        : `最近一次扫描 · ${stamp(scan.finished_at)}`;

  return (
    <div className="runpanel" id="opppanel" hidden={!open}>
      <div className="rp-h">
        <b>
          {!scan ? "还没有扫描记录"
            : failed ? "扫描失败"
              : running ? "正在扫描" : "扫描完成"}
        </b>
        {scan ? (
          <span className="rp-el">{running ? "已用 " : "用时 "}{mmss(Math.round(seconds))}</span>
        ) : null}
        {scan && stages.length && !failed ? (
          <span className="rp-el">
            {Math.round(progress * 100)}%{current ? " · " + current.label : ""}
          </span>
        ) : null}
        <span className="spacer" />
      </div>
      <div className="rp-bar">
        <i style={{ transform: "scaleX(" + progress.toFixed(4) + ")" }} />
      </div>
      <ol className="rp-steps">
        {stages.map((stage) => (
          <li className={"rst " + stage.state} key={stage.key}>
            <Marker state={stage.state as "pending" | "run" | "done" | "failed"} />
            <b>{stage.label}</b>
            {/* 阶段内的进度：一个六分钟的 collect 与一次六十问的 classify，只有"running"
                两个字的时候，页面看起来就是卡住了（user, 2026-10-18）。 */}
            {stage.state === "run" && stage.total ? (
              <span className="rst-n">{stage.done ?? 0}/{stage.total}</span>
            ) : null}
            <span className="rst-r">{result(stage.key)}</span>
            <em className="rst-t">
              {stageTime(stage.seconds)}
            </em>
          </li>
        ))}
      </ol>
      <div className="rp-foot">
        {note ? note + " · " : ""}{foot}
      </div>
    </div>
  );
}

/** 正文：一句"是什么" + 步骤 + 风险，固定高度，超出由右下角的「展开」打开。
 *
 * 报告卡的结论就是这一套（`Card.tsx` 的 `Verdict`）：**折叠状态下整张卡只有这一块可变**，
 * 所以卡片才等高；按钮只在真的被切时才出现（`scrollHeight > clientHeight` 实测，不是按字数
 * 猜），而展开时跳过测量 —— 否则"测量→按钮消失→高度回来→又被切"会让按钮闪。
 *
 * 展开状态**不记住**：刷新回到折叠，与雷达一致（user, 2026-10-18）。批量展开（顶栏
 * 展开卡片）走 `full`，并且那张卡自己的按钮就不再出现 —— 已经没有东西可展开了。
 */
function Body({ item, full }: { item: OpportunityItem; full: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const [cut, setCut] = useState(false);
  const [open, setOpen] = useState(false);
  const expanded = full || (open && cut);
  const grown = useRef(expanded);
  grown.current = expanded;
  const more = cut && !full;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => {
      if (grown.current) return;
      setCut(el.scrollHeight > el.clientHeight + 1);
    };
    measure();
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, [item.slug, full]);

  return (
    <div className={"opp-body-wrap" + (expanded ? " full" : "") + (cut && !expanded ? " cut" : "")}>
      <div className="opp-body" ref={box}>
        {item.summary ? <p className="opp-sum">{item.summary}</p> : null}
        {item.steps.length ? (
          <ol className="opp-steps">
            {item.steps.map((step) => (
              <li key={step.ordinal}>
                <b>{step.title || `步骤 ${step.ordinal + 1}`}</b>
                <span>{step.body}</span>
                {step.source_url ? (
                  <a href={step.source_url} target="_blank" rel="noreferrer">
                    来源 <Icon as={ExternalLink} size={12} />
                  </a>
                ) : null}
              </li>
            ))}
          </ol>
        ) : null}
        {item.risk ? <p className="opp-risk">风险：{item.risk}</p> : null}
      </div>
      {more ? (
        <div className="opp-body-more">
          <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "收起" : "展开"}
            <Icon as={ChevronDown} size={14} />
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** One entry. Steps for an activity, a sentence for a project - and the trust state, which is
 *  three-valued on purpose: 未判定 is a reading, not a missing one. */
function EntryCard({ item, trust, full, onState }: {
  item: OpportunityItem;
  trust: Record<string, { label: string; tone: string }>;
  full: boolean;
  onState: (slug: string, state: string) => void;
}) {
  const badge = trust[item.trust] ?? { label: item.trust, tone: "muted" };
  return (
    <article className="card opp-card">
      {/* Two lines, always: the name and its chips, then everything that is a reading about
          it - where it came from, when it was found, and the two badges. The badges ride the
          meta line rather than the head because a long title plus four chips wraps, and a
          wrapped badge row reads as a second heading. They **lead** that line instead of being
          pushed to its far end, which wraps into a right-aligned row of its own. */}
      <div className="opp-head">
        <b className="opp-title">{item.title}</b>
        {/* 分类与成熟度是**标签**，所以它们要待在 `.chips` 里：`display:inline-flex` 与居中
            只写在 `.chips .chip` 上（proto.css），裸着放会落回 26px、`line-height:1` 的
            那个版本 —— 文字顶在框顶，看着像一个空盒子（user, 2026-10-18）。 */}
        {item.category || item.maturity ? (
          <span className="srow-v chips">
            {item.category ? <span className="chip">{item.category}</span> : null}
            {item.maturity ? <span className="chip">{item.maturity}</span> : null}
          </span>
        ) : null}
      </div>
      <div className="opp-meta">
        <span className="opp-badges">
          {/* 热度口径挂在这一颗徽章上（页面不再为它单开一行）：它是**来源帖自身**的
              互动量，不是全站计数，只作徽章与筛选、不做排序键。 */}
          <span className={pillClass(heatTone(item.heat_band))}
            title={"热度＝来源帖自身的互动量（不是全站计数），只作徽章与筛选，不做排序键。"
              + (item.heat_basis ? " " + item.heat_basis : " 这份材料没有互动读数。")}>
            {item.heat_band}{item.heat === null || item.heat === undefined ? "" : " " + item.heat}
          </span>
          <span className={pillClass(badge.tone)} title={item.trust_basis ?? ""}>
            {badge.label}
          </span>
        </span>
        {item.organizer ? <span>@{item.organizer}</span> : null}
        {item.official_url ? (
          <a href={item.official_url} target="_blank" rel="noreferrer">
            官方入口 <Icon as={ExternalLink} size={12} />
          </a>
        ) : null}
        <span>发现 {stamp(item.discovered_at)}</span>
        {item.started_at ? <span>开始 {item.started_at}</span> : null}
        <span>{item.deadline ? "截止 " + item.deadline : "截止 未公布"}</span>
      </div>
      <Body item={item} full={full} />
      {/* 门槛行**永远在**：抽不到门槛是「未判定」——那是这个模块自己的一个读数（与热度的
          未判定同一套说法），不是"这一条没有门槛"。它同时让折叠态的卡片少一处高度差。 */}
      <div className="srow-v chips" title={item.barrier.join(" · ")}>
        {item.barrier.length ? (
          <>
            {item.barrier.slice(0, 2).map((name) => (
              <span className="chip" key={name}>{name}</span>
            ))}
            {item.barrier.length > 2 ? (
              <span className="chip">+{item.barrier.length - 2}</span>
            ) : null}
          </>
        ) : (
          <span className="chip">门槛 未判定</span>
        )}
      </div>
      {/* 不在列表上的条目自己说明为什么：未晋升的原因、或已结束的原因。 */}
      {item.status !== "promoted" && item.reason ? (
        <p className="opp-reason">{item.reason}</p>
      ) : null}
      <div className="opp-foot">
        <span>{item.trust_basis}</span>
        {item.source_ref ? (
          <a href={item.source_ref} target="_blank" rel="noreferrer">原文</a>
        ) : null}
        {item.extract_version ? <span>{item.extract_version}</span> : null}
        {/* 这一页唯一的写动作，而且是读者自己的：归档＝知道但不优先，删除＝不想要（软删，
            行与证据都留着）。它不动管线那半 —— 卡片照样说清"为什么它不在列表上"。 */}
        <span className="opp-acts">
          <button type="button"
            title={item.user_state === "archived" ? "取消归档" : "归档（知道但不优先，留在归档里）"}
            onClick={() => onState(item.slug, item.user_state === "archived" ? "active" : "archived")}>
            <Icon as={Archive} size={12} />
          </button>
          <button type="button" title="删除（软删，可从已删除恢复）"
            onClick={() => onState(item.slug, "deleted")}>
            <Icon as={Trash2} size={12} />
          </button>
        </span>
      </div>
    </article>
  );
}

export function OpportunityView() {
  const query = useOpportunity();
  const sources = useOpportunitySources();
  const scanNow = useStartOpportunityScan();
  const stopScan = useCancelOpportunityScan();
  const setState = useSetOpportunityState();
  const settings = useOpportunitySettingsState();
  /** 报告 / 设定：与雷达顶栏那条一样，是这一页的两面。 */
  const [view, setView] = useState<"report" | "settings">(
    () => readPref(VIEW_KEY) === "settings" ? "settings" : "report",
  );
  useEffect(() => { savePref(VIEW_KEY, view); }, [view]);
  /** 列表 tab：全部（除归档/删除之外的一切）· 本轮 · 归档 · 删除 · 排队中。 */
  const [tab, setTab] = useState<"all" | "round" | "archived" | "deleted" | "queues">("all");
  /** 时间轮次条选中的那一轮；选了就只看它新增的。 */
  const [pickedScan, setPickedScan] = useState<string>("");
  const scans = useOpportunityScans();
  const queues = useOpportunityQueues();
  const [panelOpen, setPanelOpen] = useState(false);
  /** 顶栏的 展开卡片 / 收起卡片：一次落到每一张卡上。卡片自己的展开是**不受控**的
   *  （读者拥有它），所以批量动作给卡片换一个 key 重挂，而不是去接管那个状态 —— 与雷达、
   *  收藏页同一条做法（`Card.tsx` 的 `openAll` / `bulkTick`）。 */
  const [openAll, setOpenAll] = useState(false);
  const [bulkTick, setBulkTick] = useState(0);
  const [error, setError] = useState("");
  /** One value per filter group, `""` meaning 全部. 状态 opens on 可参与: the list is what the
   *  page is for, and the pool behind it is one click away. */
  const [picked, setPicked] = useState<Record<string, string>>({ status: "promoted" });
  const [sortKey, setSortKey] = useState("discovered");
  const [dir, setDir] = useState("desc");
  const [search, setSearch] = useState("");

  const data = query.data;
  const form: OpportunityForm = data?.form ?? {};
  const groups: OpportunityFilter[] = data?.filters ?? [];
  const sorts = data?.sorts ?? [];
  const trustLabels = form.trust ?? {};
  const items = data?.items ?? [];
  const counts = (data?.counts ?? {}) as Record<string, number>;
  const running = data?.scan?.status === "running";
  const value = (key: string) => picked[key] ?? "";

  const scanRows = scans.data ?? [];
  const latest = scanRows[0];
  const roundStart = (scan: string) => scanRows.find((row) => row.id === scan)?.started_at ?? "";
  const roundEnd = (scan: string) => {
    // 一轮的窗口 = 它自己的开始 到 下一轮（更早那条）的开始；最早那轮的下界是空串。
    const index = scanRows.findIndex((row) => row.id === scan);
    const next = index >= 0 ? scanRows[index + 1] : undefined;
    return next?.started_at ?? "9999";
  };

  const inTab = (item: OpportunityItem) => {
    if (pickedScan) {
      const from = roundStart(pickedScan);
      return item.discovered_at >= from && item.discovered_at < roundEnd(pickedScan);
    }
    if (tab === "archived") return item.user_state === "archived";
    if (tab === "deleted") return item.user_state === "deleted";
    if (tab === "round") {
      return item.user_state === "active" && !!latest
        && item.discovered_at >= latest.started_at;
    }
    return item.user_state === "active";
  };

  const tabCounts = {
    all: items.filter((item) => item.user_state === "active").length,
    round: latest
      ? items.filter((item) => item.user_state === "active"
        && item.discovered_at >= latest.started_at).length
      : 0,
    archived: items.filter((item) => item.user_state === "archived").length,
    deleted: items.filter((item) => item.user_state === "deleted").length,
    queues: (queues.data?.unclassified.count ?? 0) + (queues.data?.unextracted.count ?? 0),
  };

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const rows = items.filter((item) => {
      if (!inTab(item)) {
        return false;
      }
      if (needle) {
        const haystack = [item.title, item.organizer, item.category, item.summary]
          .filter(Boolean).join(" ").toLowerCase();
        if (!haystack.includes(needle)) {
          return false;
        }
      }
      return groups.every((group) => matches(item, group.key, value(group.key)));
    });
    const sign = dir === "asc" ? 1 : -1;
    const sorted = [...rows].sort((a, b) => {
      if (sortKey === "deadline") {
        // 没有截止时间的排最后，两个方向都一样：它是"材料没说"，不是"最早"也不是"最晚"
        // （与 收藏 页对没有分数的卡同一条规则）。
        const left = a.deadline || "";
        const right = b.deadline || "";
        if (!left && !right) return 0;
        if (!left) return 1;
        if (!right) return -1;
        return left < right ? -sign : left > right ? sign : 0;
      }
      const left = a.discovered_at || "";
      const right = b.discovered_at || "";
      return left < right ? -sign : left > right ? sign : 0;
    });
    return sorted;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, groups, picked, search, sortKey, dir, tab, pickedScan, scans.data]);

  const sortNow = sorts.find((option) => option.value === sortKey) ?? sorts[0];
  const dirs = sortNow?.dirs ?? [];

  const runSettings = () => {
    setError("");
    void settings.commit().then((saved) => {
      if (!saved) return;
      setPanelOpen(true);
      scanNow.mutate(undefined, { onError: (exc) => setError(errorText(exc)) });
    });
  };

  return (
    <div id="view-opportunity">
      <header className="topbar">
        {/* 与雷达顶栏那条一样：这一页有两面，报告是列表，设定是旋钮。 */}
        <div className="tabs">
          <span className={"tab" + (view === "report" ? " on" : "")}
            onClick={() => setView("report")}>
            <Icon as={FileText} />
            报告
          </span>
          <span className={"tab" + (view === "settings" ? " on" : "")}
            onClick={() => setView("settings")}>
            <Icon as={SlidersHorizontal} />
            设定
          </span>
        </div>
        <div className="acts">
          <button
            className="btn sec"
            aria-expanded={panelOpen}
            title={panelOpen ? "收起运行结果" : "展开运行结果"}
            onClick={() => setPanelOpen((open) => !open)}
          >
            <Icon as={ListChecks} size={14} />
            运行结果
            <Icon as={panelOpen ? ChevronUp : ChevronDown} size={14} />
          </button>
          {view === "report" ? (
            <>
              {/* 与雷达、收藏、搜索三页同一个批量动作：一个标签两种状态，读者学一次，而且它
                  永远说的是"下一次点击会做什么"。图标也沿用那三页的
                  `UnfoldVertical` / `FoldVertical`（2026-10-18）。 */}
              <button type="button" className="btn sec"
                onClick={() => { setOpenAll((open) => !open); setBulkTick((tick) => tick + 1); }}>
                <Icon as={openAll ? FoldVertical : UnfoldVertical} size={14} />
                {openAll ? "收起卡片" : "展开卡片"}
              </button>
              {/* 与 收藏 / 搜索 两页同一个输入框：边打边筛，不落库也不记进 prefs。 */}
              <input
                className="fav-search"
                value={search}
                placeholder="标题 / 账号 / 类目"
                spellCheck={false}
                onChange={(event) => setSearch(event.target.value)}
              />
            </>
          ) : (
            <>
              <button type="button" className="btn sec"
                disabled={settings.save.isPending || settings.query.isLoading
                  || settings.query.isError || !settings.ready}
                onClick={() => void settings.commit()}>
                <Icon as={Save} size={14} />
                保存并生效
              </button>
              <button type="button" className="btn"
                disabled={running || scanNow.isPending || settings.save.isPending
                  || settings.query.isLoading || settings.query.isError || !settings.ready}
                onClick={runSettings}>
                {running || scanNow.isPending
                  ? <Icon as={LoaderCircle} size={14} className="spin" />
                  : <Icon as={Play} size={14} />}
                {running ? "扫描中…" : "立即运行"}
              </button>
            </>
          )}
          {/* 中止只在真的能中止时出现：它停在下一次 X 调用上，而不是等这一次调用回来。 */}
          {running ? (
            <button type="button" className="btn sec" disabled={stopScan.isPending}
              onClick={() => stopScan.mutate(data?.scan?.id ?? "", {
                onError: (exc) => setError(errorText(exc)),
              })}>
              <Icon as={Ban} size={14} />
              中止
            </button>
          ) : null}
        </div>
      </header>

      <ScanPanel scan={data?.scan} open={panelOpen} form={form} />

      {error ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>{error}</span>
        </div>
      ) : null}
      {view === "settings" ? <OpportunitySettings state={settings} /> : (
      <>
      {query.isError ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>{errorText(query.error)}</span>
        </div>
      ) : null}

      {/* 五个 tab：全部（除归档/删除之外的一切）· 本轮 · 归档 · 删除 · 排队中。
          读者自己的决定与管线那半分开记，所以这里筛的是 user_state。 */}
      <div className="opp-bar">
        <div className="tabs">
          {([["all", "全部"], ["round", "本轮"], ["archived", "归档"],
             ["deleted", "删除"], ["queues", "排队中"]] as const).map(([key, label]) => (
            <span key={key}
              className={"tab" + (!pickedScan && tab === key ? " on" : "")}
              onClick={() => { setTab(key); setPickedScan(""); }}>
              {label} {tabCounts[key]}
            </span>
          ))}
        </div>
        <span className="opp-counts">
          {pickedScan
            ? `只看第 ${scanRows.findIndex((row) => row.id === pickedScan) + 1} 轮`
            : `共 ${items.length} 条条目`}
        </span>
      </div>

      {/* 时间轮次条：每一轮一格，点一格看那一轮新增的；再点一次回到全部。
          它与「归档」是同一份数据 —— 条是快选，列表是细看。 */}
      {scanRows.length ? (
        <div className="opp-strip">
          {scanRows.slice(0, 12).map((row, index) => {
            const entries = (row.counters as { extract?: { entries?: number } }).extract?.entries ?? 0;
            const stamp = (row.started_at || "").slice(5, 16).replace("T", " ");
            return (
              <button type="button" key={row.id}
                className={"opp-round" + (pickedScan === row.id ? " on" : "")}
                title={`${row.started_at} · 出卡 ${entries} 条 · ${row.status}`}
                onClick={() => {
                  setPickedScan(pickedScan === row.id ? "" : row.id);
                  setTab("all");
                }}>
                <b>{index === 0 ? "本轮" : `第 ${index + 1} 轮`}</b>
                <span>{stamp}</span>
                <em>{entries} 条</em>
              </button>
            );
          })}
        </div>
      ) : null}

      <div className="grid fav-body">
        <aside className="fav-side">
          {groups.map((group) => (
            <Fragment key={group.key}>
              <h4 className="look-h">{group.title}</h4>
              <div className="fav-opts">
                {group.options.map((option) => (
                  <div
                    key={option.value || "all"}
                    className={"fav-opt" + (value(group.key) === option.value ? " on" : "")}
                    onClick={() => setPicked((prev) => ({ ...prev, [group.key]: option.value }))}
                  >
                    {option.label}
                    {/* 只有 状态 这一组带计数：那三个数原来在页头，面板不该把它弄丢。 */}
                    {option.count === null || option.count === undefined
                      ? null
                      : <span className="opp-opt-count">{option.count}</span>}
                  </div>
                ))}
              </div>
            </Fragment>
          ))}
          <h4 className="look-h">排序</h4>
          <div className="fav-opts">
            {sorts.map((option) => (
              <div
                key={option.value}
                className={"fav-opt" + (sortKey === option.value ? " on" : "")}
                onClick={() => {
                  setSortKey(option.value);
                  setDir(option.dirs[0]?.[0] ?? "desc");
                }}
              >
                <Icon as={SORT_ICONS[option.value] ?? Clock} size={14} />
                {option.label}
              </div>
            ))}
          </div>
          {/* 方向永远两行、随选中的排序换文案：面板高度不变（与 收藏 / 搜索 同一条规则）。 */}
          <div className="fav-opts fav-dirs">
            {dirs.map(([dirValue, label]) => (
              <div
                key={dirValue}
                className={"fav-opt" + (dir === dirValue ? " on" : "")}
                onClick={() => setDir(dirValue)}
              >
                <Icon as={DIR_ICONS[dirValue] ?? ArrowDownWideNarrow} size={14} />
                {label}
              </div>
            ))}
          </div>
        </aside>

        <section className="fav-cards">
          {tab === "queues" ? (
            <div className="opp-queues">
              <div className="note-bar">
                <span>
                  还在排队的东西：它们已经抓到了，只是还没走完分析 —— 这里回答
                  「我明明见过某个项目，它去哪了」。
                </span>
              </div>
              {([
                ["unclassified", "未分组", "抓到了，还没交给模型分组"],
                ["unextracted", "未抽取", "已经分组，还没排到抽取（每轮有限额）"],
              ] as const).map(([key, title, lead]) => {
                const pile = queues.data?.[key];
                return (
                  <div className="opp-pile" key={key}>
                    <h4 className="opp-list-title">
                      {title}（{pile?.count ?? 0}）
                      <span className="opp-hint">{lead}</span>
                    </h4>
                    <table className="opp-table">
                      <tbody>
                        {(pile?.top ?? []).map((row) => (
                          <tr key={row.ref}>
                            <td className="opp-pile-score">{Math.round(row.score)}</td>
                            <td>@{row.handle}</td>
                            <td>{(row.text || "").slice(0, 90)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                );
              })}
              <div className="opp-pile">
                <h4 className="opp-list-title">
                  溯源失败（{queues.data?.trace_failed.count ?? 0}）
                  <span className="opp-hint">没找到官方来源，进了未晋升池</span>
                </h4>
              </div>
            </div>
          ) : shown.length ? (
            <div className="grid">
              {shown.map((item) => (
                <EntryCard item={item} trust={trustLabels} full={openAll}
                  onState={(slug, state) => setState.mutate({ slug, state })}
                  key={item.slug + ":" + bulkTick} />
              ))}
            </div>
          ) : (
            <div className="note-bar">
              {items.length ? "没有符合当前筛选的条目。" : form.empty}
            </div>
          )}
          {/* 「分析中」在「全部」的末尾：排队中的东西信息量与卡片差一个量级，混排会让
              列表忽长忽短，所以它是一行摘要 + 一个折叠区。 */}
          {tab === "all" && !pickedScan ? (
            <details className="opp-fold">
              <summary>
                <Icon as={Inbox} size={12} />
                分析中（{(queues.data?.unclassified.count ?? 0)
                  + (queues.data?.unextracted.count ?? 0)}）
              </summary>
              <p className="opp-hint">
                已抓到、还没走完分析：未分组 {queues.data?.unclassified.count ?? 0} ·
                未抽取 {queues.data?.unextracted.count ?? 0} ·
                溯源失败 {queues.data?.trace_failed.count ?? 0}。
                切到「排队中」看分数最高的那几条。
              </p>
            </details>
          ) : null}
        </section>
      </div>

      {/* The watchlist, read-only: rows are added by editing the seed or the table, and a
          control here that could not save would be the silent switch this app does not ship. */}
      <details className="opp-fold">
        <summary>
          {form.sources_title ?? "watchlist"}（{sources.data?.length ?? 0}）
        </summary>
        <p className="opp-hint">{form.sources_lead}</p>
        {sources.isError ? (
          <div className="note-bar bad">
            <Icon as={TriangleAlert} size={14} />
            <span>{errorText(sources.error)}</span>
          </div>
        ) : (
          <table className="opp-table">
            <thead>
              <tr><th>目标</th><th>类型</th><th>窗口 / 片长</th><th>说明</th></tr>
            </thead>
            <tbody>
              {(sources.data ?? []).map((row) => (
                <tr key={row.target}>
                  <td>{row.target}</td>
                  <td>{row.kind === "query" ? "查询" : "官方账号"}</td>
                  <td>
                    {row.kind === "query"
                      ? `${row.window_hours}h / ${row.slice_hours}h 一片`
                      : "—"}
                  </td>
                  <td>{row.note ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </details>
      </>
      )}

      {view === "report" ? (
        <div className="opp-tally">
          已读帖子 {counts.posts ?? 0} · watchlist {counts.sources ?? 0}
          （账号 {sources.data?.filter((row) => row.kind === "official").length ?? 0}
          · 查询 {sources.data?.filter((row) => row.kind === "query").length ?? 0}）
        </div>
      ) : null}
    </div>
  );
}
