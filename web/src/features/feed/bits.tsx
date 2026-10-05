import { ChevronDown, ChevronRight, ExternalLink, Minus, TrendingDown, TrendingUp } from "lucide-react";
import { useState } from "react";

import type { FeedDisplayTag, FeedOtherReport, FeedTaxonomy } from "../../api/client";
import { Icon } from "../../components/Icon";
import { Collapse } from "./Presence";

/** 页面印在数字旁边的那些词, 全部来自后端 (CLAUDE.md 2.3.3). 这里只负责把它们取出来,
 *  取不到时留空而不是编一个 —— 一个没有名字的数是数据还没到, 不是"该写个默认值". */
export type Labels = FeedTaxonomy["labels"];

export function word(labels: Labels | undefined, key: string): string {
  return labels?.[key] ?? "";
}

/** 卡片 / 详情页顶上那一行角标: 先说牵了谁 (实体, 点得动), 再说是什么 (分类标签, 点不动).
 *
 *  两串并成一串是后端做的 (`display_tags`), 页面只管画 —— 实体那一格是按钮, 点开是主题页.
 *  后端还没升级、或者这一条没赶上新字段时退回 `tags`: 少一个可点, 不少一行字.
 */
export function DisplayTags({ tags, fallback, limit, onOpenTopic }: {
  tags?: FeedDisplayTag[];
  fallback: string[];
  limit: number;
  onOpenTopic?: (name: string) => void;
}) {
  const list: FeedDisplayTag[] = tags && tags.length
    ? tags
    : fallback.map((text) => ({ text, entity: false }));
  return (
    <>
      {list.slice(0, limit).map((tag) =>
        tag.entity ? (
          <button type="button" className="it-tag it-tag-ent" key={tag.text}
                  onClick={() => onOpenTopic?.(tag.text)}>{tag.text}</button>
        ) : (
          <span className="it-tag" key={tag.text}>{tag.text}</span>
        ),
      )}
    </>
  );
}

/** 热度线: 24 个小时格子 (25 个点), 缺的小时是 `null` —— 线在那里断开, 不是补一个 0.
 *  格子读的是落库的小时快照, 所以最后一个点必定等于旁边那颗数字 —— 两处不一致的图
 *  比没有图更糟 (snapshot_heat.sparkline). */
export function Sparkline({ points, width = 92, height = 24 }: {
  points: (number | null)[];
  width?: number;
  height?: number;
}) {
  const real = points.filter((value): value is number => value !== null);
  if (real.length < 2) return null;
  const top = Math.max(...real, 1);
  const step = width / (points.length - 1);
  const at = (value: number) => height - 1.5 - (value / top) * (height - 3);
  let pen = false;
  const line = points
    .map((value, index) => {
      if (value === null) {
        pen = false;
        return "";
      }
      const move = pen ? "L" : "M";
      pen = true;
      return `${move}${(index * step).toFixed(1)} ${at(value).toFixed(1)}`;
    })
    .filter(Boolean)
    .join(" ");
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`}
         role="img" aria-label="24 小时走势（每小时一个点）">
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

/** 走势: 五态由后端判 (AIHOT 的 `trend`), 页面只翻成词 —— 阈值改了, 词跟着改 (CLAUDE.md 2.3.3).
 *
 *  `unknown` = 有旧的, 但可比的那组是空的 (有的源正在落后): 这一眼先不比较, 不是"没涨";
 *  `new` = 六小时前这件事还不存在, 不是一个很大的百分比 —— 读者要知道的是"这是新事"还是
 *  "这事在涨", 而那两句话不能由同一个数字说出来.
 */
export function Trend({ trend, pct, labels }: {
  trend: string | undefined;
  pct: number | null | undefined;
  labels: Labels | undefined;
}) {
  if (trend === "unknown") {
    return <span className="trend unknown">{word(labels, "unknown")}</span>;
  }
  if (trend === "new" || pct === null || pct === undefined) {
    return <span className="trend new">{word(labels, "new")}</span>;
  }
  if (trend === "flat") {
    return (
      <span className="trend flat">
        <Icon as={Minus} size={12} />
        {word(labels, "flat")}
      </span>
    );
  }
  const up = trend === "up";
  return (
    <span className={"trend " + (up ? "up" : "down")}>
      <Icon as={up ? TrendingUp : TrendingDown} size={12} />
      {Math.abs(Math.round(pct))}%
    </span>
  );
}

/** 热点榜标题上那几枚小标签 (heat.badges): `surge` / `new` / `rising`, 可以同时出现两枚.
 *
 *  它不是一个可以自己算的东西: 三条规则("这 6 小时加入的来源过半", "还不到 6 小时",
 *  "比 6 小时前涨了 15%")都在后端 (heat.py), 页面只把 key 翻成一个词. 一个 key 认不出就
 *  不画 —— 画一个空白的小方块比不画更糟.
 */
export function HotBadge({ badges, labels }: {
  badges: string[] | undefined;
  labels: Labels | undefined;
}) {
  const shown = (badges ?? [])
    .map((badge) => ({ badge, text: word(labels, "badge_" + badge) }))
    .filter((item) => item.text);
  if (!shown.length) return null;
  return (
    <>
      {shown.map((item) => (
        <span key={item.badge} className={"hot-badge " + item.badge}>{item.text}</span>
      ))}
    </>
  );
}

/** 「另有 N 家信源报道」: 同一件事里别家怎么说它, 不是"谁引了谁". 折叠行印的那个数来自后端
 *  (`other_report_count`, 是**信源家数**), 展开是每家自己那条的标题与时刻 —— 列表最多 5 条, 所以
 *  一句话都不许由这里算: 拿被截断的长度去印, 折叠行就会撒谎.
 *
 *  标题通向那一条自己的页 (和卡片标题同一条路), 右边那个箭头才跳去来源: 前者是"读我们把它读成
 *  了什么", 后者是"读它自己的原话", 两件事不共用同一个点击.
 */
export function OtherReports({ reports, count, labels, onOpenItem }: {
  reports: FeedOtherReport[];
  count: number;
  labels: Labels | undefined;
  onOpenItem: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (count <= 0) return null;
  return (
    <div className="or">
      <button type="button" className="or-toggle" aria-expanded={open}
              onClick={() => setOpen((prev) => !prev)}>
        <Icon as={open ? ChevronDown : ChevronRight} size={12} />
        {word(labels, "other_reports").replace("%d", String(count))}
      </button>
      <Collapse open={open}>
        <ul className="or-list">
          {reports.map((report) => (
            <li key={report.id}>
              <span className="or-src">{report.source_name}</span>
              <button type="button" className="or-title" onClick={() => onOpenItem(report.id)}>
                {report.title}
              </button>
              <span className="or-time">{report.time_label}</span>
              <a className="or-out" href={report.url} target="_blank" rel="noreferrer"
                 aria-label={word(labels, "open_original")}>
                <Icon as={ExternalLink} size={12} />
              </a>
            </li>
          ))}
        </ul>
      </Collapse>
    </div>
  );
}
