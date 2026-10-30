/** 监控：一条长期问题，自己抓、自己一套卡、自己一页（2026-10-29, user）。
 *
 *  报告页是一个列表 + 一个面板；设定页没有左栏，一列就是每个监控一块 —— 和「新机会」设定页
 *  的 采集 / 分析 / 查询与闸 同一种排法。两个开关各管一件事：项目启用决定它存不存在于报告，
 *  计划任务只决定按不按点自动跑（手动运行始终可用）。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Fragment } from "react";
import {
  ArrowDownWideNarrow, ArrowUpNarrowWide, CalendarClock, ChevronDown, Clock,
  FileText, FoldVertical, ListChecks, Play,
  Radar, SlidersHorizontal, UnfoldVertical,
} from "lucide-react";
import {
  useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactElement,
} from "react";

import { putMonitorSettings, type Monitor, type MonitorCard, type MonitorScan } from "../../api/client";
import {
  useCancelMonitorScan,
  useMonitorCards,
  useMonitors,
  useStartMonitorScan,
} from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { readPref, savePref } from "../../prefs";
import { Marker } from "../run/RunPanel";
import { NoteHelp } from "../settings/NoteHelp";

//: 刷新要回到同一页：选中的监控与 报告 / 设定 都记在这里（与雷达的 ch.* 同一套）。
const PICK_KEY = "ch.monitor.pick";
const TAB_KEY = "ch.monitor.tab";

const STATUS: { key: string; label: string }[] = [
  { key: "upcoming", label: "未开始" },
  { key: "live", label: "铸造中" },
  { key: "ended", label: "已结束" },
  { key: "unknown", label: "未判定" },
  { key: "none", label: "不适用" },
];

//: 类型筛选：和卡面 chip 同一套 key；未判定的类型只在真的出现时才列出来。
const TYPES: { key: string; label: string }[] = [
  { key: "nft", label: "NFT" },
  { key: "share", label: "代币份额" },
  { key: "inscription", label: "铭文 / 符文" },
  { key: "launchpad", label: "发射台" },
  { key: "task", label: "任务 / 积分" },
];

const kindOf = (card: MonitorCard) => String(card.fields?.type ?? "") || "unknown";

const TRUST: Record<string, { label: string; tone: string }> = {
  official: { label: "官方", tone: "ok" },
  community: { label: "社区（未互证）", tone: "warn" },
  unjudged: { label: "未判定", tone: "muted" },
};
//: 类型按「你买到什么」分（2026-10-30, user）。未知的不出 chip，也不掏空卡面。
const KIND: Record<string, string> = {
  nft: "NFT",
  share: "代币份额",
  inscription: "铭文 / 符文",
  launchpad: "发射台",
  task: "任务 / 积分",
};
//: 铸造状态按「还能不能参与」上色：绿=现在能做，黄=值得等，灰=来不及，虚线灰=没读到。
const STATE: Record<string, string> = {
  live: "chip-live",
  upcoming: "chip-soon",
  ended: "chip-done",
  unknown: "chip-dashed",
};

/** 一张卡只有一个状态：铸造类按进度，非铸造类（做任务 / 发射台）落未判定。 */
function cardState(card: MonitorCard): { key: string; label: string; tone: string; why: string } {
  // 发射台 / 任务没有铸造这一说：先按类型判定，别让 card.status 把它们算进铸造中
  // （SoulBoard 被计进「铸造中」，2026-10-30）。
  const type = String(card.fields?.type ?? "");
  if (type === "launchpad" || type === "task") {
    return { key: "none", label: "不适用", tone: "muted", why: "发射台 / 任务型，不按铸造进度计" };
  }
  const mint = (card.fields?.mint ?? {}) as {
    kind?: string; state?: string; done?: number | null; total?: number | null;
  };
  const kind = String(mint.kind ?? "");
  const done = mint.done != null ? Number(mint.done) : null;
  const total = mint.total != null ? Number(mint.total) : null;
  if (kind && kind !== "mint") {
    return {
      key: "unknown", label: "未判定", tone: "muted",
      why: kind === "task" ? "做任务型，不按铸造进度计"
        : kind === "platform" ? "发射台，不按铸造进度计" : "不是铸造项目",
    };
  }
  if (mint.state === "sold_out" || (done != null && total != null && done >= total)) {
    return { key: "ended", label: "已结束", tone: "muted", why: "" };
  }
  // 数字先说话：只要读到已经有人铸造，页面上的「白名单 / 未开卖」就是过期说法。
  if (mint.state === "live" || (done != null && done > 0)) {
    return { key: "live", label: "铸造中", tone: "ok", why: "" };
  }
  if (mint.state === "not_started") {
    return { key: "upcoming", label: "未开始", tone: "warn", why: "" };
  }
  // 链上和官网都没给数字时，用抽取阶段的项目状态兜底：页面写「尚未开放」就是未开始，
  // 不该落成未判定（PaluAi / AgentSea，2026-10-30）。
  const status = String(card.status ?? "").toLowerCase();
  if (status === "upcoming") {
    return { key: "upcoming", label: "未开始", tone: "warn", why: "项目页写的是尚未开放" };
  }
  if (status === "ended") {
    return { key: "ended", label: "已结束", tone: "muted", why: "项目页写的是已结束" };
  }
  if (status === "live") {
    return { key: "live", label: "铸造中", tone: "ok", why: "项目页写的是进行中" };
  }
  return { key: "unknown", label: "未判定", tone: "muted", why: "没有读到铸造数字" };
}

type Draft = {
  enabled: boolean;
  schedule: boolean;
  interval_hours: string;
  start_hour: string;
  start_minute: string;
  window_hours: string;
  slice_hours: string;
  min_score: string;
};

function pillClass(tone: string): string {
  return tone ? `pill pill-${tone}` : "pill";
}

function stamp(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "—";
}

function shortStamp(value?: string | null): string {
  const text = stamp(value);
  const year = String(new Date().getFullYear());
  return text.startsWith(year + "-") ? text.slice(5) : text;
}

function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value > 0 && value < 0.5) return "<1s";
  return `${Math.floor(value / 60)}:${String(Math.round(value % 60)).padStart(2, "0")}`;
}

/** 一次列表就把每个监控的旋钮带回来了，草稿从它初始化。 */
function draftsFrom(list: Monitor[]): Record<string, Draft> {
  const out: Record<string, Draft> = {};
  for (const monitor of list) {
    const values = (monitor.settings ?? {}) as {
      enabled?: boolean; schedule?: boolean; interval_hours?: number;
      start_hour?: number; start_minute?: number;
      window_hours?: number; slice_hours?: number; min_score?: number;
    };
    out[monitor.id] = {
      enabled: values.enabled ?? true,
      schedule: values.schedule ?? true,
      interval_hours: String(values.interval_hours ?? 8),
      start_hour: String(values.start_hour ?? 1),
      start_minute: String(values.start_minute ?? 15),
      window_hours: String(values.window_hours ?? 24),
      slice_hours: String(values.slice_hours ?? 4),
      min_score: String(values.min_score ?? 30),
    };
  }
  return out;
}

function MonitorPanel({ scan, open, onCancel }: {
  scan: MonitorScan | null | undefined;
  open: boolean;
  onCancel?: () => void;
}) {
  const stages = (scan?.stages ?? []) as {
    key: string; label: string; state: string;
    seconds?: number | null; done?: number | null; total?: number | null;
  }[];
  const counters = (scan?.counters ?? {}) as Record<string, number | Record<string, number>>;
  const done = stages.filter((stage) => stage.state === "done").length;
  const progress = stages.length ? done / stages.length : 0;
  const running = scan?.status === "running";
  const traced = (counters.traced ?? {}) as Record<string, number>;
  // 落 C 的项目：找到站点没有、读到几页、二次搜索跑没跑（2026-10-30 诊断用）。
  const skipped = Array.isArray(counters.skipped_items)
    ? (counters.skipped_items as unknown as {
      title: string; handle: string; site: string; pages: number; searched: boolean;
    }[])
    : [];

  const reading = (key: string): string => {
    if (key === "collect") {
      return counters.posts === undefined ? "—" : `帖子 ${counters.posts} · 调用 ${counters.calls ?? 0}`;
    }
    if (key === "gate") return counters.gated === undefined ? "—" : `过闸 ${counters.gated}`;
    if (key === "extract") return counters.projects === undefined ? "—" : `项目 ${counters.projects}`;
    if (key === "trace") {
      return counters.traced === undefined
        ? "—"
        : `A ${traced.A ?? 0} · B ${traced.B ?? 0} · C ${traced.C ?? 0}`;
    }
    if (key === "chain") return counters.probed === undefined ? "—" : `已查 ${counters.probed}`;
    if (key === "store") return counters.written === undefined ? "—" : `写入 ${counters.written}`;
    if (key === "refresh") {
      return counters.refreshed === undefined
        ? "—"
        : `更新 ${counters.refreshed} · 未变 ${counters.unchanged ?? 0}`;
    }
    return "—";
  };

  return (
    <div className="runpanel" hidden={!open}>
      <div className="rp-h">
        <b>
          {!scan ? "本轮 · 待运行"
            : running ? "正在扫描 · 本轮"
              : scan.status === "failed" ? "本轮失败"
                : scan.status === "cancelled" ? "已中止 · 本轮" : "扫描完成 · 本轮"}
        </b>
        {scan?.trigger === "auto" ? <span className="rp-el">自动</span> : null}
        <span className="spacer" />
        {running && onCancel ? (
          <button type="button" className="btn sec" onClick={onCancel}>中止</button>
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
            <span className="rst-r">{reading(stage.key)}</span>
            <em className="rst-t">{seconds(stage.seconds)}</em>
          </li>
        ))}
      </ol>
      <div className="rp-foot">
        {scan?.error
          ? "扫描失败：" + scan.error
          : scan
            ? `帖子 ${counters.posts ?? 0} → 过闸 ${counters.gated ?? 0} → 项目 ${counters.projects ?? 0}`
              + ` · 官方 ${traced.A ?? 0} / 未互证 ${traced.B ?? 0}`
              + (counters.skipped ? ` · 跳过 ${counters.skipped}` : "")
            : "还没有扫描过"}
      </div>
      {skipped.length ? (
        <details className="rp-skip">
          <summary>跳过 {String(counters.skipped ?? 0)} 个 · 看原因</summary>
          <ul>
            {skipped.map((item) => (
              <li key={item.title + ":" + item.handle}>
                <b>{item.title}</b>
                {item.handle ? ` @${item.handle}` : ""}
                {` · 官网 ${item.site || "没找到"} · 读到 ${item.pages} 页`
                  + ` · 二次搜索 ${item.searched ? "跑了" : "没跑"}`}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/** 正文：固定三行，超出由右下角的「展开」打开；测量与裁切沿用新机会卡那一套。 */
function Body({ text, full }: { text: string; full: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const [cut, setCut] = useState(false);
  const [open, setOpen] = useState(false);
  const expanded = cut && (full || open);
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
  }, [text, full]);

  return (
    <div className={"opp-body-wrap" + (expanded ? " full" : "")
      + (expanded && !full ? " single" : "") + (cut && !expanded ? " cut" : "")}>
      <div className="opp-body" ref={box}>
        <p className="opp-sum">{text}</p>
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

function AgentIdCard({ card, full }: { card: MonitorCard; full: boolean }) {
  const trust = TRUST[card.trust ?? "unjudged"] ?? TRUST.unjudged;
  const st = cardState(card);
  const kind = String(card.fields?.type ?? "");
  const start = String(card.fields?.start ?? "");
  // 发射台和任务没有可买的东西：只留项目说明，不摆铸造价 / 进度。
  const sells = kind !== "launchpad" && kind !== "task";
  const sources = (card.sources ?? []) as { kind: string; label: string; url: string }[];
  // `onchain` 是链上那一组；早期写的行把它放在 `chain` 上过，读的时候兼容一下。
  const stored = (card.fields?.onchain ?? card.fields?.chain ?? null) as unknown;
  const info = (stored && typeof stored === "object"
    ? stored : null) as Record<string, number | string | boolean> | null;
  const mint = (card.fields?.mint ?? null) as {
    done?: number | null; total?: number | null; unit?: string;
    token_total?: number | null; source?: string; state?: string;
  } | null;
  // 官网写着「还没开卖 / 白名单阶段」时，已铸造就是 0 —— 这是读到的状态，不是猜的数。
  const declared = String(mint?.state ?? "");
  const mintDone = mint?.done != null
    ? Number(mint.done)
    : (declared === "not_started" ? 0 : null);
  const mintTotal = mint?.total != null ? Number(mint.total) : null;
  const pct = mintDone != null && mintTotal
    ? Math.min(100, (mintDone / mintTotal) * 100) : null;
  // 铸造自己的状态，和「这轮机会是否还 live」是两件事：数字都在才判得了。
  // 代币地址是链上验证过的那种（symbol / decimals / totalSupply 全会答），铸造合约不是代币：
  // 卡上不印合约，只有代币才给 GMGN / Debot，铸造合约不顶替（2026-10-30, user）。
  const token = info?.token ? String(info.token) : "";
  // 页脚和币种卡同一种排法：GMGN ｜ Debot ｜ X ｜ 官网，竖线分隔、不带图标（2026-10-30, user）。
  const foot: { label: string; url: string }[] = [];
  if (token && kind === "share" && String(card.chain).toUpperCase() === "BSC") {
    foot.push({ label: "GMGN", url: `https://gmgn.ai/bsc/token/${token}` });
    foot.push({ label: "Debot", url: `https://debot.ai/token/bsc/${token}` });
  }
  for (const source of sources) {
    if (source.kind !== "official_account" && source.kind !== "official_site") continue;
    if (foot.some((item) => item.url === source.url)) continue;
    foot.push({ label: source.kind === "official_account" ? "X" : "官网", url: source.url });
  }
  // 后端可能把从官网抄来的份数存成 "6,624" 这种字符串；能转成数字就按数字印。
  const count = (value: unknown) => {
    const number = typeof value === "number" ? value : Number(String(value ?? "").replace(/,/g, ""));
    return Number.isFinite(number) ? number.toLocaleString() : "未判定";
  };
  const money = (value: unknown) => {
    if (typeof value !== "number") return "未判定";
    if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
    if (value >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
    return `$${value.toFixed(0)}`;
  };
  // 安全三枚：貔貅、LP 能不能被撤、合约权限。读不到就写未判定，不拿「无标记」冒充安全。
  const chip = (tone: string, label: string, why: string) => ({ tone, label, why });
  const tax = Math.max(
    typeof info?.buy_tax === "number" ? info.buy_tax : 0,
    typeof info?.sell_tax === "number" ? info.sell_tax : 0);
  const pools = info && typeof info.pools === "number" ? info.pools : null;
  const rug = (info?.rug ?? {}) as { state?: string; before?: number; after?: number };
  const perms = Array.isArray(info?.perms) ? (info?.perms as string[]) : null;
  const PERM: Record<string, string> = {
    mintable: "可增发", blacklist: "黑名单", pausable: "可暂停",
    hidden_owner: "隐藏 owner", owner_change_balance: "owner 可改余额",
    can_take_back_ownership: "可收回所有权", is_proxy: "代理合约",
  };
  const hp = (() => {
    // 千分之几的税是噪音，不到 1% 不占位置；到 10% 标黄、20% 标红。
    const taxNote = tax >= 1 ? ` · 税 ${tax}%` : "";
    if (info?.honeypot === true) return chip("danger", "貔貅", "GoPlus 模拟买卖：只能买不能卖");
    if (info?.honeypot === false) {
      return chip(tax >= 20 ? "danger" : tax >= 10 ? "warn" : "ok", "非貔貅" + taxNote,
        `GoPlus 模拟买卖都能过${tax >= 1 ? `；买卖税最高 ${tax}%` : ""}`);
    }
    if (pools === 0) return chip("muted", "无池", "还没上池，貔貅要在池子里才测得出");
    return chip("muted", "未判定", "GoPlus 没有回答");
  })();
  const lp = (() => {
    if (rug.state === "gone") {
      return chip("danger", "已撤池", "上一轮还有池子，这一轮没了");
    }
    if (rug.state === "drained") {
      const drop = Math.round((1 - (rug.after ?? 0) / (rug.before || 1)) * 100);
      return chip("danger", `流动性 -${drop}%`,
        `上一轮 ${money(rug.before)} → 这一轮 ${money(rug.after)}`);
    }
    const state = String(info?.lp_state ?? "");
    const share = typeof info?.lp_locked_pct === "number" ? info.lp_locked_pct : null;
    if (state === "empty") return chip("muted", "未组池", "池子已建但还没有流动性");
    if (state === "burnt") return chip("ok", "LP 已烧", "LP 全在黑洞地址，撤不了");
    if (state === "locked") return chip("ok", `LP 已锁 ${share}%`, "LP 锁在锁仓合约，撤不了");
    if (state === "partial") return chip("warn", `LP 锁 ${share}%`, "一半以上锁住，仍有可撤的部分");
    if (state === "unlocked") {
      return chip("danger", "LP 未锁", `只有 ${share ?? 0}% 锁住，部署者随时能抽走`);
    }
    if (pools === 0) return chip("muted", "无池", "还没上池");
    return chip("muted", "LP 未判定", "GoPlus 没给 LP 表，池子里也没有能核对的锁仓份额");
  })();
  const perm = (() => {
    if (perms?.length) {
      const names = perms.map((key) => PERM[key] ?? key);
      return chip("danger", names[0] + (names.length > 1 ? ` +${names.length - 1}` : ""),
        names.join("、"));
    }
    // 合约没有 owner 的「可增发」就是公开铸造，谁都关不掉，也谁都偷不走。
    if (info?.mint_public === true) {
      return chip("muted", "公开铸造", "铸造还开着，合约没有 owner，不存在单方面增发");
    }
    if (perms) return chip("ok", "无危险权限", "没命中增发 / 暂停 / 黑名单这类开关");
    return chip("muted", "未判定", "GoPlus 没有回答合约权限");
  })();
  // 没上池时「貔貅」和「LP」都是同一句无池，只留一枚；权限也没答就不摆空壳。
  const safeChips = pools === 0 ? (perms ? [hp, perm] : [hp]) : [hp, lp, perm];
  return (
    <article className="card opp-card mon-card">
      <div className="opp-head">
        <b className="opp-title">{card.title}</b>
        {sells ? (
          <span className="srow-v chips">
            <span className={`chip ${STATE[st.key] ?? ""}`} title={st.why}>{st.label}</span>
          </span>
        ) : null}
      </div>
      <div className="opp-meta">
        <span className={pillClass(trust.tone)} title={card.trust_basis ?? ""}>
          {trust.label}
        </span>
        {/* 用标题行同一套 20px 小 chip，别让裸 `.chip` 的 26px 在这行里凸出来。 */}
        <span className="srow-v chips">
          <span className="chip">{card.chain || "未判定"}</span>
        </span>
        {KIND[kind] ? (
          <span className="srow-v chips">
            <span className="chip">{KIND[kind]}</span>
          </span>
        ) : null}
      </div>
      <div className="opp-time">
        <span>发现 {shortStamp(card.first_seen)}</span>
        <span>{start ? "开始 " + shortStamp(start) : "开始 未公布"}</span>
        <span>{card.deadline ? "截止 " + shortStamp(card.deadline) : "截止 未公布"}</span>
      </div>
      {sells ? (
        <div className="mon-facts">
          <span>铸造价</span>
          <b>{card.price || "未判定"}</b>
          {mint ? null : (
            <>
              <span>供应</span>
              <b>{card.supply || "未判定"}</b>
            </>
          )}
        </div>
      ) : null}
      {mint && sells ? (
        <div className="mon-mint">
          <div className="mon-mint-line">
            <span>铸造进度</span>
            <b>
              {mintDone != null
                ? count(mintDone) + (mint.total ? ` / ${count(mint.total)}` : "")
                : mint.total ? `总量 ${count(mint.total)}` : "未判定"}
              {pct != null ? `（${pct.toFixed(1)}%）` : ""}
              {mint.unit ? ` ${mint.unit}` : ""}
            </b>
          </div>
          {pct != null ? <i className="mon-bar"><u style={{ width: pct.toFixed(1) + "%" }} /></i> : null}
        </div>
      ) : null}
      {info && token && kind === "share" ? (
        <div className="mon-chain mon-sec">
          <span>安全</span>
          <b>
            {safeChips.map((item) => (
              <span className={pillClass(item.tone)} title={item.why} key={item.label}>
                {item.label}
              </span>
            ))}
          </b>
        </div>
      ) : null}
      <Body text={card.summary ?? ""} full={full} />
      <div className="opp-foot">
        {foot.map((link, index) => (
          <Fragment key={link.url}>
            {index > 0 ? " ｜ " : null}
            <a href={link.url} target="_blank" rel="noreferrer">{link.label}</a>
          </Fragment>
        ))}
      </div>
    </article>
  );
}

const CARDS: Record<string, (props: { card: MonitorCard; full: boolean }) => ReactElement> = {
  "binance-agent-id": AgentIdCard,
};

export function MonitorsView() {
  const monitors = useMonitors();
  const client = useQueryClient();
  const [picked, setPicked] = useState(() => readPref(PICK_KEY) ?? "");
  const [tab, setTab] = useState<"report" | "settings">(
    () => (readPref(TAB_KEY) === "settings" ? "settings" : "report"),
  );
  const [panel, setPanel] = useState(false);
  const [bulk, setBulk] = useState(false);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<"found" | "deadline">("found");
  const [sortDir, setSortDir] = useState<"desc" | "asc">("desc");
  const [search, setSearch] = useState("");
  const [drafts, setDrafts] = useState<Record<string, Draft> | null>(null);

  const list = monitors.data?.monitors ?? [];
  const enabled = useMemo(() => list.filter((row) => row.enabled), [list]);
  const current = enabled.find((row) => row.id === picked) ?? enabled[0];

  useEffect(() => {
    if (!picked && enabled.length) setPicked(enabled[0].id);
  }, [enabled, picked]);

  useEffect(() => { savePref(TAB_KEY, tab); }, [tab]);
  // 选中的监控以页面上真正显示的那个为准：原来的那个被关掉时，落回来的第一个也会被记住。
  useEffect(() => { if (current?.id) savePref(PICK_KEY, current.id); }, [current?.id]);

  useEffect(() => {
    if (list.length) setDrafts((prev) => prev ?? draftsFrom(list));
  }, [list]);

  const changed = useMemo(() => {
    if (!drafts || !list.length) return 0;
    const base = draftsFrom(list);
    return list.filter(
      (monitor) => JSON.stringify(drafts[monitor.id]) !== JSON.stringify(base[monitor.id]),
    ).length;
  }, [drafts, list]);
  const dirty = changed > 0;

  const saveAll = useMutation({
    mutationFn: async () => {
      for (const monitor of list) {
        const draft = drafts?.[monitor.id];
        if (!draft) continue;
        await putMonitorSettings(monitor.id, {
          enabled: draft.enabled,
          schedule: draft.schedule,
          interval_hours: Number(draft.interval_hours) || 0,
          start_hour: Number(draft.start_hour) || 0,
          start_minute: Number(draft.start_minute) || 0,
          // 查询只写在 config/monitors/<id>.yaml 里：页面不发送，保存也就不会覆盖它。
          window_hours: Number(draft.window_hours) || 0,
          slice_hours: Number(draft.slice_hours) || 0,
          min_score: Number(draft.min_score) || 0,
        });
      }
    },
    onSuccess: () => {
      setDrafts(null);
      client.invalidateQueries({ queryKey: ["monitors"] });
    },
  });

  const cards = useMonitorCards(current?.id ?? "");
  const start = useStartMonitorScan(current?.id ?? "");
  const stop = useCancelMonitorScan(current?.id ?? "");
  const scan = current?.scan ?? null;
  const rows = cards.data?.cards ?? [];

  // 类型计数是全局的；状态计数跟着已选类型走，两个筛选相加才对得上（2026-10-30, user）。
  const typeCounts = useMemo(() => {
    const out: Record<string, number> = { "": rows.length };
    for (const row of rows) {
      const key = kindOf(row);
      out[key] = (out[key] ?? 0) + 1;
    }
    return out;
  }, [rows]);

  const counts = useMemo(() => {
    const scoped = kind ? rows.filter((row) => kindOf(row) === kind) : rows;
    const out: Record<string, number> = { "": scoped.length };
    for (const row of scoped) {
      const key = cardState(row).key;
      out[key] = (out[key] ?? 0) + 1;
    }
    return out;
  }, [rows, kind]);

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const filtered = rows.filter((row) => {
      if (kind && kindOf(row) !== kind) return false;
      if (status && cardState(row).key !== status) return false;
      if (!needle) return true;
      return [row.title, row.summary, row.chain, row.handle]
        .filter(Boolean).join(" ").toLowerCase().includes(needle);
    });
    const direction = sortDir === "asc" ? 1 : -1;
    return [...filtered].sort((left, right) => {
      if (sort === "deadline") {
        const a = left.deadline || "";
        const b = right.deadline || "";
        if (!a && !b) return 0;
        if (!a) return 1;   // 没有截止的永远排最后，方向不影响
        if (!b) return -1;
        return a.localeCompare(b) * direction;
      }
      return (left.first_seen || "").localeCompare(right.first_seen || "") * direction;
    });
  }, [rows, kind, status, sort, sortDir, search]);

  const Card = current ? (CARDS[current.id] ?? AgentIdCard) : AgentIdCard;

  return (
    <div id="view-monitors">
      <header className="topbar">
        <div className="tabs">
          <span className={"tab" + (tab === "report" ? " on" : "")} onClick={() => setTab("report")}>
            <Icon as={FileText} />
            报告
          </span>
          <span className={"tab" + (tab === "settings" ? " on" : "")} onClick={() => setTab("settings")}>
            <Icon as={SlidersHorizontal} />
            设定
          </span>
        </div>
        <div className="acts">
          <button type="button" className="btn sec" onClick={() => setPanel((open) => !open)}
                  aria-expanded={panel}>
            <Icon as={ListChecks} size={14} />
            运行结果
          </button>
          {tab === "report" ? (
            <>
              <button type="button" className="btn sec" aria-pressed={bulk}
                      onClick={() => setBulk((open) => !open)}>
                <Icon as={bulk ? FoldVertical : UnfoldVertical} size={14} />
                {bulk ? "收起卡片" : "展开卡片"}
              </button>
              <input className="fav-search" value={search} placeholder="搜索项目 / 链 / 账号"
                     onChange={(event) => setSearch(event.target.value)} />
              <button type="button" className="btn" disabled={!current || current.running || start.isPending}
                      onClick={() => start.mutate()}>
                <Icon as={Play} size={14} />
                {current?.running ? "扫描中…" : "立即运行"}
              </button>
            </>
          ) : (
            <button type="button" className="btn" disabled={!dirty || saveAll.isPending}
                    onClick={() => saveAll.mutate()}>
              {saveAll.isPending ? "保存中…" : "保存并生效"}
            </button>
          )}
        </div>
      </header>

      {tab === "report" ? (
        <MonitorPanel scan={scan} open={panel}
                      onCancel={() => { if (scan) stop.mutate(scan.id); }} />
      ) : null}

      {tab === "settings" ? (
        <section className="params opp-settings mon-settings">
          <div className="dirtybar" hidden={!dirty}>
            <b>参数已改 <i>{changed}</i> 项</b>
            {" —— 保存并生效后，下一轮扫描使用新值。"}
          </div>
          {saveAll.isError ? (
            <div className="note-bar bad"><span>保存失败</span></div>
          ) : null}
          {list.map((monitor) => {
            const draft = drafts?.[monitor.id];
            if (!draft) return null;
            const off = !draft.enabled;
            const set = (patch: Partial<Draft>) =>
              setDrafts((prev) => (prev ? { ...prev, [monitor.id]: { ...draft, ...patch } } : prev));
            return (
              <div className={"blk" + (off ? " off" : "")} key={monitor.id}>
                <div className="blk-t">
                  <Icon as={Radar} size={14} />
                  <b>{monitor.name}</b>
                </div>
                <div className="blk-cols mon-blk-cols">
                  <div className="pgroup">
                    <div className="pgroup-h">
                      <b>开关</b>
                      <NoteHelp text={"启用：决定这个监控是否出现在报告页；关掉后卡片隐藏、自动任务停止。\n计划任务：只决定按不按点自动跑；手动「立即运行」始终可用。"} />
                    </div>
                    <div className="prows">
                      <div className="prow">
                        <label>启用</label>
                        <span className="field">
                          <span className="chk">
                            <input type="checkbox" checked={draft.enabled}
                                   onChange={(event) => set({ enabled: event.target.checked })} />
                          </span>
                        </span>
                      </div>
                      <div className="prow">
                        <label>计划任务</label>
                        <span className="field">
                          <span className="chk">
                            <input type="checkbox" disabled={off} checked={draft.schedule}
                                   onChange={(event) => set({ schedule: event.target.checked })} />
                          </span>
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="pgroup">
                    <div className="pgroup-h"><b>运行节奏</b></div>
                    <div className="prows">
                      <div className="prow">
                        <label>间隔（小时）</label>
                        <span className="field">
                          <input className="num" type="number" min={0} max={168} disabled={off}
                                 value={draft.interval_hours}
                                 onChange={(event) => set({ interval_hours: event.target.value })} />
                          <span className="unit">0–168</span>
                        </span>
                      </div>
                      <div className="prow">
                        <label>起始（时）</label>
                        <span className="field">
                          <input className="num" type="number" min={0} max={23} disabled={off}
                                 value={draft.start_hour}
                                 onChange={(event) => set({ start_hour: event.target.value })} />
                          <span className="unit">0–23</span>
                        </span>
                      </div>
                      <div className="prow">
                        <label>起始（分）</label>
                        <span className="field">
                          <input className="num" type="number" min={0} max={59} disabled={off}
                                 value={draft.start_minute}
                                 onChange={(event) => set({ start_minute: event.target.value })} />
                          <span className="unit">0–59</span>
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="pgroup">
                    <div className="pgroup-h">
                      <b>采集与过闸</b>
                      <NoteHelp text={"查询窗口：往后读多少小时。\n切片长度：每次 X 调用覆盖多少小时，窗口按它切开。\n过闸下限分：低于这个分数的帖子不进模型。\nX 查询本身写在 config/monitors/<id>.yaml 里，不在这个页面。"} />
                    </div>
                    <div className="prows">
                      <div className="prow">
                        <label>查询窗口（小时）</label>
                        <span className="field">
                          <input className="num" type="number" min={1} max={720} disabled={off}
                                 value={draft.window_hours}
                                 onChange={(event) => set({ window_hours: event.target.value })} />
                          <span className="unit">1–720</span>
                        </span>
                      </div>
                      <div className="prow">
                        <label>切片长度（小时）</label>
                        <span className="field">
                          <input className="num" type="number" min={1} max={168} disabled={off}
                                 value={draft.slice_hours}
                                 onChange={(event) => set({ slice_hours: event.target.value })} />
                          <span className="unit">1–168</span>
                        </span>
                      </div>
                      <div className="prow">
                        <label>过闸下限分</label>
                        <span className="field">
                          <input className="num" type="number" min={0} max={200} disabled={off}
                                 value={draft.min_score}
                                 onChange={(event) => set({ min_score: event.target.value })} />
                          <span className="unit">0–200</span>
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </section>
      ) : (
        <div className="grid fav-body">
          <aside className="fav-side">
            <div className="fold-h">
              <div className="fold-btn"><span className="fold-name">项目</span></div>
            </div>
            <div className="fav-opts">
              {enabled.map((monitor) => (
                <div key={monitor.id}
                     className={"fav-opt" + (monitor.id === current?.id ? " on" : "")}
                     onClick={() => { setPicked(monitor.id); setKind(""); setStatus(""); }}>
                  <span className="mon-name">{monitor.name}</span>
                  <span className="opp-opt-count">{monitor.cards}</span>
                </div>
              ))}
            </div>

            <div className="fold-h">
              <div className="fold-btn"><span className="fold-name">类型</span></div>
            </div>
            <div className="fav-opts">
              <div className={"fav-opt" + (kind === "" ? " on" : "")} onClick={() => setKind("")}>
                <span>全部</span>
                <span className="opp-opt-count">{typeCounts[""] ?? 0}</span>
              </div>
              {TYPES.map((row) => (
                <div key={row.key}
                     className={"fav-opt" + (kind === row.key ? " on" : "")}
                     onClick={() => setKind(row.key)}>
                  <span>{row.label}</span>
                  <span className="opp-opt-count">{typeCounts[row.key] ?? 0}</span>
                </div>
              ))}
              {typeCounts.unknown ? (
                <div className={"fav-opt" + (kind === "unknown" ? " on" : "")}
                     onClick={() => setKind("unknown")}>
                  <span>未判定</span>
                  <span className="opp-opt-count">{typeCounts.unknown}</span>
                </div>
              ) : null}
            </div>

            <div className="fold-h">
              <div className="fold-btn"><span className="fold-name">状态</span></div>
            </div>
            <div className="fav-opts">
              <div className={"fav-opt" + (status === "" ? " on" : "")} onClick={() => setStatus("")}>
                <span>全部</span>
                <span className="opp-opt-count">{counts[""] ?? 0}</span>
              </div>
              {STATUS.map((row) => (
                <div key={row.key}
                     className={"fav-opt" + (status === row.key ? " on" : "")}
                     onClick={() => setStatus(row.key)}>
                  <span>{row.label}</span>
                  <span className="opp-opt-count">{counts[row.key] ?? 0}</span>
                </div>
              ))}
            </div>

            <div className="fold-h">
              <div className="fold-btn"><span className="fold-name">排序</span></div>
            </div>
            <div className="fav-opts">
              <div className={"fav-opt" + (sort === "found" ? " on" : "")} onClick={() => setSort("found")}>
                <Icon as={Clock} size={14} />
                <span>发现时间</span>
              </div>
              <div className={"fav-opt" + (sort === "deadline" ? " on" : "")} onClick={() => setSort("deadline")}>
                <Icon as={CalendarClock} size={14} />
                <span>截止时间</span>
              </div>
            </div>
            <div className="fav-opts fav-dirs">
              <div className={"fav-opt" + (sortDir === "desc" ? " on" : "")}
                   onClick={() => setSortDir("desc")}>
                <Icon as={ArrowDownWideNarrow} size={14} />
                <span>从新到旧</span>
              </div>
              <div className={"fav-opt" + (sortDir === "asc" ? " on" : "")}
                   onClick={() => setSortDir("asc")}>
                <Icon as={ArrowUpNarrowWide} size={14} />
                <span>从旧到新</span>
              </div>
            </div>
          </aside>

          <section className="fav-cards">
            {current ? (
              <>
                <div className="mon-strip">
                  <span>{scan ? `上次扫描 ${stamp(scan.started_at)}` : "还没有扫描过"}</span>
                  <span>{shown.length} / {rows.length} 张</span>
                </div>
                <div className="grid">
                  {shown.map((card) => <Card key={card.slug} card={card} full={bulk} />)}
                </div>
                {!shown.length ? (
                  <div className="note-bar">
                    {rows.length ? "没有符合条件的项目。" : "还没有卡片。点「立即运行」跑一轮。"}
                  </div>
                ) : null}
              </>
            ) : (
              <div className="note-bar">没有启用的监控。到设定页打开一个。</div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
