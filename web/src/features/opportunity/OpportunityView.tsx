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
  Pause,
  CalendarClock,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Clock,
  ExternalLink,
  FileText,
  FoldVertical,
  ListChecks,
  LoaderCircle,
  Play,
  RotateCw,
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
    case "category_group":
      return item.category_group === value;
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
const NOVELTY_LABELS: Record<string, string> = {
  protocol: "新协议", gameplay: "新玩法", mechanism: "新机制", standard: "新标准",
  asset: "新资产", chain: "新链/生态", tooling: "新工具", market: "新市场",
};

function stamp(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return value.slice(0, 16).replace("T", " ");
}

/** 卡片时间行的那一版：今年的日期省掉年份，只留 `MM-DD HH:mm`，三个时间才放得进一行。 */
function shortStamp(value: string | null | undefined): string {
  const text = stamp(value);
  if (text === "—") {
    return "";
  }
  const year = String(new Date().getFullYear());
  return text.startsWith(year + "-") ? text.slice(5) : text;
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
function ScanPanel({ scan, open, stopping, onCancel }: {
  scan: OpportunityScan | null | undefined;
  open: boolean;
  stopping?: boolean;
  onCancel?: () => void;
}) {
  const stages = scan?.stages ?? [];
  const ended = stages.filter((stage) => stage.state === "done").length;
  const progress = stages.length ? ended / stages.length : 0;
  const failed = scan?.status === "failed";
  const cancelled = scan?.status === "cancelled";
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
        ? `X 查询 ${counters.queries ?? 0}`
        : "—";
    }
    if (key === "refresh") {
      // 官网是另一条 lane：站点数、这轮读了几家、几家真的变了。不和 X 查询挤在一格里。
      const sites = (counters.refresh ?? {}) as {
        sites?: number; registered?: number; read?: number; changed?: number; failed?: number;
      };
      if (sites.sites === undefined) {
        return "—";
      }
      return `站点 ${sites.registered ?? sites.sites} · 读 ${sites.read ?? 0}`
        + ` · 变 ${sites.changed ?? 0}` + (sites.failed ? ` · 失败 ${sites.failed}` : "");
    }
    if (key === "collect") {
      // 这一格报的是 X 调用次数与过闸条数：查询行一条会变成好几次调用（切片），而
      // 预算花的是调用不是行。
      return collect.posts === undefined
        ? "—"
        : `帖子 ${collect.posts} · 过闸 ${collect.gated ?? 0} · 排除 ${collect.excluded ?? 0}`
          + ` · X 调用 ${collect.queries ?? 0}`
          + (collect.calls_failed ? ` · 失败 ${collect.calls_failed}` : "");
    }
    if (key === "classify") {
      if (classify.groups !== undefined) {
        return `分组 ${classify.groups} · 无关 ${classify.irrelevant ?? 0}`
          + ` · 待定 ${classify.uncertain ?? 0}`;
      }
      return classify.asked === undefined ? "—" : `问了 ${classify.asked} 条`;
    }
    if (key === "trace") {
      const trace = (counters.trace ?? {}) as {
        traced?: Record<string, number>;
        skipped?: number;
        failed?: number;
      };
      if (trace.traced === undefined) return "—";
      return `A ${trace.traced.A ?? 0} · B ${trace.traced.B ?? 0}`
        + ` · C ${trace.traced.C ?? 0}`
        + (trace.skipped ? ` · 跳过 ${trace.skipped}` : "")
        + (trace.failed ? ` · 失败 ${trace.failed}` : "");
    }
    if (key === "extract") {
      return extract.entries === undefined
        ? "—"
        : `条目 ${extract.entries} · 晋升 ${extract.promoted ?? 0}`;
    }
    if (key === "chain") {
      const chain = (counters.chain ?? {}) as Record<string, number>;
      return chain.cards === undefined
        ? "—"
        : `待补 ${chain.cards} · 补上 ${chain.found ?? 0}`
          + ` · 搜索 ${chain.searched ?? 0}`;
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
  /** 与 Arc 一轮同一条：第一下只是上膛，第二下才真的停；10 秒不动自动退回。 */
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) {
      return;
    }
    const timer = window.setTimeout(() => setArmed(false), 10000);
    return () => window.clearTimeout(timer);
  }, [armed]);
  // 与 Arc 一轮同一条读法：一句漏斗 + 一句状态，不重复轮次条上已有的时间。
  const funnel = collect.posts === undefined
    ? ""
    : `帖子 ${collect.posts} → 分组 ${classify.groups ?? 0} → 出卡 ${extract.entries ?? 0}`;
  const foot = !scan
    ? "本轮待运行 · 跑完自动落库"
    : failed
      ? "扫描失败：" + (scan.error ?? "未记录原因")
      : cancelled
        ? (funnel ? funnel + " · 本次已中止" : "本次已中止")
        : running
          ? (funnel ? funnel + " · 跑完自动落库" : "正在扫描 · 跑完自动落库")
          : (funnel ? funnel + " · 阶段结果取数据库里的真实状态"
            : `最近一次扫描 · ${stamp(scan.finished_at)}`);

  return (
    <div className="runpanel" id="opppanel" hidden={!open}>
      <div className="rp-h">
        <b>
          {!scan ? "本轮 · 待运行"
            : failed ? "本轮失败"
              : cancelled ? "已中止 · 本轮"
                : running ? "正在扫描 · 本轮" : "扫描完成 · 本轮"}
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
        {scan && running ? (
          stopping ? (
            <span className="btn sec demo">
              <Icon as={LoaderCircle} className="spin" size={14} />
              正在中止
            </span>
          ) : (
            <button
              className={armed ? "btn sec arm" : "btn sec"}
              onClick={armed
                ? () => { setArmed(false); onCancel?.(); }
                : () => setArmed(true)}
              title={armed
                ? "再点一次：扫描停在当前阶段，已读到的材料保留"
                : "停止这次扫描：停在当前阶段，已读到的材料保留"}
            >
              <Icon as={Pause} size={14} />
              {armed ? "确认中止" : "中止"}
            </button>
          )
        ) : null}
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
    <div className={"opp-body-wrap" + (expanded ? " full" : "")
      + (expanded && !full ? " single" : "")
      + (cut && !expanded ? " cut" : "")}>
      <div className="opp-body" ref={box}>
        {item.summary ? <p className="opp-sum">{item.summary}</p> : null}
        {item.steps.length ? (
          <ol className="opp-steps">
            {item.steps.map((step) => (
              <li key={step.ordinal}>
                <b>{step.title || `步骤 ${step.ordinal + 1}`}</b>
                <span>{step.body}</span>
                {step.source_url ? (
                  <a className="opp-step-src" href={step.source_url} target="_blank"
                     rel="noreferrer" title="这一步的来源" aria-label="步骤来源">
                    <Icon as={ExternalLink} size={12} />
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
  const handle = (item.organizer || "").replace(/^@/, "");
  // 类目与新机制常常是同一个词（新协议 / 新协议），一条事实只画一次。
  const chips = [...new Set([
    item.category,
    item.maturity,
    item.novelty ? (NOVELTY_LABELS[item.novelty] ?? item.novelty) : "",
    // 没有抽到动作的卡只能看，不能做：它是一个读数，不该和「可参与」混在一起看不出来。
    item.steps.length ? "" : "仅观察",
  ].filter(Boolean))];
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
        {chips.length ? (
          <span className="srow-v chips">
            {chips.map((name) => <span className="chip" key={name}>{name}</span>)}
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
        {handle ? (
          <a href={"https://x.com/" + handle} target="_blank" rel="noreferrer">@{handle}</a>
        ) : null}
        {item.official_url ? (
          <a href={item.official_url} target="_blank" rel="noreferrer">
            官方入口 <Icon as={ExternalLink} size={12} />
          </a>
        ) : null}
      </div>
      {/* 时间单独一行：发现 / 开始 / 截止是同一种读数，不该和账号、入口挤在一起。 */}
      <div className="opp-time">
        <span>发现 {shortStamp(item.discovered_at)}</span>
        {item.started_at ? <span>开始 {shortStamp(item.started_at)}</span> : null}
        <span>{item.deadline ? "截止 " + shortStamp(item.deadline) : "截止 未公布"}</span>
      </div>
      {/* 为什么它不在列表上：这是状态，跟时间放在一起，不跟动作挤在页脚。 */}
      {item.status !== "promoted" && item.reason ? (
        <p className="opp-state">
          {item.status === "retired" ? "已结束" : "未晋升"} · {item.reason}
        </p>
      ) : null}
      <Body item={item} full={full} />
      {/* 门槛行**永远在**：抽不到门槛是「未判定」——那是这个模块自己的一个读数（与热度的
          未判定同一套说法），不是"这一条没有门槛"。它同时让折叠态的卡片少一处高度差。 */}
      <div className={(item.barrier.length ? "srow-v chips" : "opp-barrier-empty") + " opp-barrier"}
           title={item.barrier.join(" · ")}>
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
          <span>门槛 未判定</span>
        )}
      </div>
      <div className="opp-foot">
        {(item.sources?.length ? item.sources : item.source_ref
          ? [{ kind: "post", label: "原文", url: item.source_ref }]
          : []).map((source, index) => (
          <a href={source.url} target="_blank" rel="noreferrer"
             key={source.kind + ":" + source.url + ":" + index}>
            {source.label} <Icon as={ExternalLink} size={12} />
          </a>
        ))}
        {/* 这一页唯一的写动作，而且是读者自己的：归档＝知道但不优先，删除＝不想要（软删，
            行与证据都留着）。它不动管线那半 —— 卡片照样说清"为什么它不在列表上"。 */}
        <span className="opp-acts">
          {item.user_state === "active" ? (
            <>
              <button type="button" title="归档（知道但不优先，管线不再更新它）"
                onClick={() => onState(item.slug, "archived")}>
                <Icon as={Archive} size={12} />
              </button>
              <button type="button" title="删除（软删，管线不再更新它）"
                onClick={() => onState(item.slug, "deleted")}>
                <Icon as={Trash2} size={12} />
              </button>
            </>
          ) : (
            <button type="button" title="恢复（回到列表，重新纳入调查）"
              onClick={() => onState(item.slug, "active")}>
              <Icon as={RotateCw} size={12} />
            </button>
          )}
        </span>
      </div>
    </article>
  );
}

/** 左侧面板的分组标题：整行可点，收起后仍报计数与当前选择，
 *  否则筛选还在生效、面板上却看不出是哪一项。 */
function FoldHead({ title, count, folded, pick, onToggle }: {
  title: string;
  count: number | null;
  folded: boolean;
  pick?: string;
  onToggle: () => void;
}) {
  return (
    <div className="look-h fold-h">
      <button type="button" className="fold-btn" aria-expanded={!folded} onClick={onToggle}>
        <Icon as={folded ? ChevronRight : ChevronDown} size={12} />
        <span className="fold-name">{title}</span>
        {folded && pick ? <span className="fold-pick">{pick}</span> : null}
        {count === null ? null : <span className="fold-count">{count}</span>}
      </button>
    </div>
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
  /** 列表状态：全部（未处理）· 归档 · 删除 · 排队中。 */
  const [tab, setTab] = useState<"all" | "archived" | "deleted" | "queues">("all");
  /** 排队中这一页的三段：左侧点哪一类就只看哪一类；切换筛选或已处理分组时回到列表。 */
  const [queueFilter, setQueueFilter] = useState<
    "all" | "unclassified" | "unextracted" | "trace_failed">("all");
  /** 时间轮次条选中的那一轮；选了就只看它新增的。 */
  const [pickedScan, setPickedScan] = useState<string>("");
  const scans = useOpportunityScans();
  const queues = useOpportunityQueues(pickedScan || undefined);
  const [panelOpen, setPanelOpen] = useState(false);
  /** 顶栏的 展开卡片 / 收起卡片：一次落到每一张卡上。卡片自己的展开是**不受控**的
   *  （读者拥有它），所以批量动作给卡片换一个 key 重挂，而不是去接管那个状态。 */
  const [openAll, setOpenAll] = useState(false);
  const [bulkTick, setBulkTick] = useState(0);
  /** 点了确认之后，按钮要一直停在「正在中止」，直到扫描行自己离开 running。 */
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  /** One value per filter group, `""` meaning 全部. 刷新后一律从「全部」开始。 */
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [sortKey, setSortKey] = useState("discovered");
  const [dir, setDir] = useState("desc");
  const [search, setSearch] = useState("");
  /** 左侧分组收起的状态：只在本页会话里记，刷新回默认。生态最长（49 条），默认收起。 */
  const [folded, setFolded] = useState<Record<string, boolean>>({ ecosystem: true });
  const toggleFold = (key: string) => setFolded((prev) => ({ ...prev, [key]: !prev[key] }));

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
  // stepper 的游标：没选轮次时停在最新一轮，选了就跟着 pickedScan 走。
  const scanIndex = pickedScan
    ? Math.max(0, scanRows.findIndex((row) => row.id === pickedScan))
    : 0;
  const currentScan = scanRows[scanIndex] ?? null;
  const pickScan = (index: number) => {
    const row = scanRows[index];
    if (!row) {
      return;
    }
    setPickedScan(row.id);
    setTab("all");
  };
  const roundStart = (scan: string) => scanRows.find((row) => row.id === scan)?.started_at ?? "";
  const roundEnd = (scan: string) => {
    // 一轮的窗口 = 它自己的开始 到 更新那一轮的开始；最新那轮的上界是「无穷」。
    // `scanRows` 是新的在前，所以「更新那条」是 index - 1，不是 + 1。
    const index = scanRows.findIndex((row) => row.id === scan);
    const next = index > 0 ? scanRows[index - 1] : undefined;
    return next?.started_at ?? "9999";
  };

  const inRound = (item: OpportunityItem) => {
    if (!pickedScan) {
      return true;
    }
    const from = roundStart(pickedScan);
    return item.discovered_at >= from && item.discovered_at < roundEnd(pickedScan);
  };

  const inTab = (item: OpportunityItem) => {
    if (!inRound(item)) {
      return false;
    }
    if (tab === "archived") return item.user_state === "archived";
    if (tab === "deleted") return item.user_state === "deleted";
    return item.user_state === "active";
  };

  // 轮次选中后，状态与排队中的计数都按这一轮重新数；否则它们会跟列表对不上。
  const roundItems = items.filter(inRound);
  const tabCounts = {
    all: roundItems.filter((item) => item.user_state === "active").length,
    archived: roundItems.filter((item) => item.user_state === "archived").length,
    deleted: roundItems.filter((item) => item.user_state === "deleted").length,
    // 三堆都是「排队中」这一页的内容，标签上的数也必须是三堆之和。
    queues: (queues.data?.unclassified.count ?? 0) + (queues.data?.unextracted.count ?? 0)
      + (queues.data?.trace_failed.count ?? 0),
  };
  // 状态计数跟着大类 / 生态的当前选择走，但不跟着状态自己走：否则选了一条链，左边状态
  // 还在报全库的数，「可参与 161」与列表里的十几张卡对不上。
  const statusScope = roundItems.filter((item) => (
    (tab === "archived" ? item.user_state === "archived"
      : tab === "deleted" ? item.user_state === "deleted"
        : item.user_state === "active")
    && matches(item, "category_group", value("category_group"))
    && matches(item, "ecosystem", value("ecosystem"))));
  /** 已处理：读者自己按下去的两个桶。「全部」是可见的退出口，和生态 / 状态同一套结构。 */
  const processedTotal = tabCounts.archived + tabCounts.deleted;
  const showProcessed = processedTotal > 0 || tab === "archived" || tab === "deleted";
  const processedRows = ([
    ["all", "全部", processedTotal],
    ["archived", "归档", tabCounts.archived],
    ["deleted", "删除", tabCounts.deleted],
  ] as const).filter(([key, , count]) => key === "all" || count > 0 || tab === key);

  /** 排队中的三项不是筛选值，是三段列表的入口：只列非零项，点了切过去并滚到那一段。 */
  const queueRows = ([
    ["unclassified", "未分组", queues.data?.unclassified.count ?? 0],
    ["unextracted", "未抽取", queues.data?.unextracted.count ?? 0],
    ["trace_failed", "溯源失败", queues.data?.trace_failed.count ?? 0],
  ] as const).filter(([key, , count]) => count > 0 || queueFilter === key);

  const openQueue = (key: "unclassified" | "unextracted" | "trace_failed") => {
    setQueueFilter(key);
    setTab("queues");
  };

  /** 面板里两个 facet（大类 / 生态）互相排除自己：否则选中的那一项会把自己从面板上删掉，
   *  其他项的计数也会被自己筛成 0。状态 / 分栏 / 搜索照常参与。 */
  const facetBase = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((item) => {
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
      return groups.every((group) => group.key === "ecosystem"
        || group.key === "category_group"
        || matches(item, group.key, value(group.key)));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, groups, picked, tab, pickedScan, scans.data, search]);

  /** 大类计数范围：除大类自己以外的筛选都算。 */
  const categoryGroupItems = useMemo(
    () => facetBase.filter((item) => matches(item, "ecosystem", value("ecosystem"))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [facetBase, picked.ecosystem],
  );
  /** 生态计数范围：除生态自己以外的筛选都算。 */
  const ecosystemItems = useMemo(
    () => facetBase.filter(
      (item) => matches(item, "category_group", value("category_group"))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [facetBase, picked.category_group],
  );

  const liveCategoryGroups = useMemo(
    () => new Set(categoryGroupItems.map((item) => item.category_group ?? "")),
    [categoryGroupItems],
  );
  const liveEcosystems = useMemo(
    () => new Set(ecosystemItems.map((item) => item.ecosystem ?? "")),
    [ecosystemItems],
  );
  const panelGroups = useMemo(() => groups.map((group) => {
    const live = group.key === "category_group" ? liveCategoryGroups
      : group.key === "ecosystem" ? liveEcosystems : null;
    return live === null ? group : {
      ...group,
      options: group.options.filter((option) => option.value === "" || live.has(option.value)),
    };
  }), [groups, liveCategoryGroups, liveEcosystems]);

  useEffect(() => {
    const chosen = picked.category_group;
    if (chosen && !liveCategoryGroups.has(chosen)) {
      setPicked((prev) => ({ ...prev, category_group: "" }));
    }
  }, [liveCategoryGroups, picked.category_group]);

  useEffect(() => {
    const chosen = picked.ecosystem;
    if (chosen && !liveEcosystems.has(chosen)) {
      setPicked((prev) => ({ ...prev, ecosystem: "" }));
    }
  }, [liveEcosystems, picked.ecosystem]);


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

  useEffect(() => {
    if (data?.scan?.status !== "running") {
      setStopping(false);
    }
  }, [data?.scan?.status]);

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
        </div>
      </header>

      {/* 运行面板紧贴顶栏，与 Arc 一轮同一条：面板 → 轮次条 → 卡片。 */}
      <ScanPanel scan={data?.scan} open={panelOpen} stopping={stopping}
                 onCancel={() => {
                   setStopping(true);
                   stopScan.mutate(data?.scan?.id ?? "", {
                     onError: (exc) => { setStopping(false); setError(errorText(exc)); },
                   });
                 }} />

      {/* 轮次条与雷达同一条：运行面板下面一行，左右箭头翻历史。 */}
      {view === "report" && currentScan ? (
        <div className="bhead opp-roundbar">
          <div className="batch">
            <button type="button" title="更早一轮"
              className={"step" + (scanIndex < scanRows.length - 1 ? "" : " demo")}
              disabled={scanIndex >= scanRows.length - 1}
              onClick={() => pickScan(scanIndex + 1)}>
              <Icon as={ChevronLeft} />
            </button>
            <b>{stamp(currentScan.started_at)}</b>
            <button type="button" title="更新一轮"
              className={"step" + (scanIndex > 0 ? "" : " demo")}
              disabled={scanIndex <= 0}
              onClick={() => pickScan(scanIndex - 1)}>
              <Icon as={ChevronRight} />
            </button>
          </div>
          <div className="funnel">
            <span>出卡</span>
            <b>{(currentScan.counters as { extract?: { entries?: number } }).extract?.entries ?? 0}</b>
          </div>
          {/* 与列表 tab 同一套分段控件：选中态是蓝底，不是一个文字链接。 */}
          <div className="tabs">
            <span className={"tab" + (pickedScan ? "" : " on")}
              onClick={() => setPickedScan("")}>
              全部轮次
            </span>
            <span className={"tab" + (pickedScan && scanIndex === 0 ? " on" : "")}
              onClick={() => pickScan(0)}>
              回到本轮
            </span>
          </div>
          <span className="spacer" />
        </div>
      ) : null}

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

      <div className="grid fav-body">
        <aside className="fav-side">
          {panelGroups.map((group) => {
            const chosen = value(group.key);
            const total = group.key === "status" ? statusScope.length
              : group.key === "ecosystem" ? ecosystemItems.length
                : group.key === "category_group" ? categoryGroupItems.length
                  : group.options.find((option) => !option.value)?.count ?? null;
            const shut = !!folded[group.key];
            return (
            <Fragment key={group.key}>
              <FoldHead title={group.title} count={total} folded={shut}
                        pick={chosen
                          ? group.options.find((option) => option.value === chosen)?.label
                          : undefined}
                        onToggle={() => toggleFold(group.key)} />
              {shut ? null : (
              <div className="fav-opts">
                {group.options.map((option) => {
                  // 状态是这一页自己的计数：选中某一轮后按那一轮重数，不能继续报全库的数。
                  // 大类与生态同理，但各自的 scope 都排除了自己，否则选中一项之后同类其他项
                  // 会全部变成 0。
                  let count = option.count;
                  if (group.key === "status") {
                    count = option.value
                      ? statusScope.filter((item) => item.status === option.value).length
                      : statusScope.length;
                  } else if (group.key === "ecosystem") {
                    count = option.value
                      ? ecosystemItems.filter((item) => item.ecosystem === option.value).length
                      : ecosystemItems.length;
                  } else if (group.key === "category_group") {
                    count = option.value
                      ? categoryGroupItems.filter(
                        (item) => item.category_group === option.value).length
                      : categoryGroupItems.length;
                  }
                  return (
                    <div
                      key={option.value || "all"}
                      className={"fav-opt" + (value(group.key) === option.value ? " on" : "")}
                      onClick={() => {
                        setPicked((prev) => ({ ...prev, [group.key]: option.value }));
                        // 在「排队中」时点任一筛选，视为要回到列表。
                        if (tab === "queues") {
                          setTab("all");
                          setQueueFilter("all");
                        }
                      }}
                    >
                      {option.label}
                      {option.type ? (
                        <span className="opp-opt-type">{option.type.toUpperCase()}</span>
                      ) : null}
                      {count === null || count === undefined
                        ? null
                        : <span className="opp-opt-count">{count}</span>}
                    </div>
                  );
                })}
              </div>
              )}
            </Fragment>
            );
          })}
          {showProcessed ? (
            <>
              <FoldHead title="已处理" count={processedTotal} folded={!!folded.processed}
                        pick={tab === "archived" ? "归档" : tab === "deleted" ? "删除" : undefined}
                        onToggle={() => toggleFold("processed")} />
              {folded.processed ? null : (
              <div className="fav-opts">
                {processedRows.map(([key, label, count]) => (
                  <div key={key}
                       className={"fav-opt" + (tab === key ? " on" : "")}
                       onClick={() => setTab(key === "all" || tab === key ? "all" : key)}>
                    {label}
                    <span className="opp-opt-count">{count}</span>
                  </div>
                ))}
              </div>
              )}
            </>
          ) : null}
          {queueRows.length ? (
            <>
              <FoldHead title="队列" count={queueRows.reduce((sum, [, , n]) => sum + n, 0)}
                        folded={!!folded.queues}
                        pick={tab === "queues"
                          ? queueRows.find(([key]) => key === queueFilter)?.[1] : undefined}
                        onToggle={() => toggleFold("queues")} />
              {folded.queues ? null : (
              <div className="fav-opts">
                {queueRows.map(([key, label, count]) => (
                  <div key={key}
                       className={"fav-opt" + (tab === "queues" && queueFilter === key ? " on" : "")}
                       onClick={() => openQueue(key)}>
                    {label}
                    <span className="opp-opt-count">{count}</span>
                  </div>
                ))}
              </div>
              )}
            </>
          ) : null}
          <FoldHead title="排序" count={null} folded={!!folded.sort}
                    pick={sorts.find((option) => option.value === sortKey)?.label}
                    onToggle={() => toggleFold("sort")} />
          {folded.sort ? null : (
          <>
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
          </>
          )}
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
                ["unextracted", "未抽取", "已经分组，等待官方解析或抽取"],
                ["trace_failed", "溯源失败", "没找到官方来源，进了未晋升池"],
              ] as const)
                .filter(([key]) => queueFilter === "all" || queueFilter === key)
                .map(([key, title, lead]) => {
                const pile = queues.data?.[key];
                return (
                  <div className="opp-pile" id={"queue-" + key} key={key}>
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
        </section>
      </div>

      {/* The watchlist, read-only: rows are added by editing the seed or the table, and a
          control here that could not save would be the silent switch this app does not ship. */}
      <details className="opp-fold">
        <summary>
          X {form.sources_title ?? "watchlist"}（{sources.data?.length ?? 0}）
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
          已读帖子 {counts.posts ?? 0} · X 查询 {counts.sources ?? 0}
          {" · "}官网 {counts.sites ?? 0}
        </div>
      ) : null}
    </div>
  );
}
