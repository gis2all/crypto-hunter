import { ChevronLeft, ChevronRight, LoaderCircle } from "lucide-react";
import { useState } from "react";

import type { FeedEdition, FeedEditionEntry } from "../../api/client";
import { errorText } from "../../api/client";
import { useFeedEdition, useFeedEditions } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { archiveGroups, clockText, dots, headlineOf, markOf, masthead, metaOf, periodText } from "./daily";
import type { FeedKind, Issue } from "./daily";

/** 日报 / 周报 / 月报: 三期一机, 印的是出刊那一刻的定稿, 不是现在这一秒重算出来的东西.
 *
 *  它是信息流里唯一读 `feed_editions` 的地方 —— 另外三页是**实时视图**(每次请求按当前窗口
 *  重算), 这三种刊是**快照**: 一期出过就不再改 (`store/db.py` 的 `feed_edition_put`: 同一段
 *  时期跑第二遍, 还回库里那一行).
 *
 *  纸上的字点开的是我们自己的事件页, 不是原文 (用户定的, 2026-10-03): 读者最先想问的是"这
 *  件事现在怎么了", 而原文那个出口在事件页底下. 于是纸和详情会**对不上** —— 纸印的是出刊那
 *  一刻的来源数与热度, 详情是现在这一秒重算的. 这个差是有意留的: 一期是那段时期的定稿, 详情
 *  是行情, 两处各印各的读数.
 *
 *  这一页是三栏, 但第三栏 (点开的那一件事) 不在这个文件里: 它和三个实时视图的右栏是同一个
 *  组件 (FeedView.tsx 的 `Pane`), 于是返回 / Esc / 压层的规矩一处都不用重写.
 *
 *  三种刊是同一条路的三个 `kind`, 所以是一个组件吃一个参数, 不是三个组件: 它们差的只有
 *  一期的长短和几个词, 而那些词全在 `daily.ts` 里按 kind 分. 版式也不复用信息流的平分两栏:
 *  中间那一列是纸, 它比右边那一列宽 (3:2) —— 均分给纸的宽度不够, 报头那一块 (刊头 ＋ 报眼 ＋
 *  指标条) 是要版面的.
 *
 *  换刊的切换器钉在档案栏顶上, 不在左栏 (用户定的, 2026-10-03): 三种刊是同一件事的三个周期,
 *  在导航里占三行等于说它们是三个地方, 而它们只是同一张报纸翻页的快慢. 选中哪一种是调用方的
 *  状态, 所以从报刊切走再回来, 看到的还是上次那一种.
 *
 *  `key` 也由调用方给, 所以换一种刊就是换一个组件实例 —— 选中的那一期是日报的 id, 换成周报
 *  就没有意义, 与其记得清空, 不如让它跟着刊一起重来.
 */

/** 一次取多少期. 左栏够长就行, 而这个数还管着"本月 N 期"那句算不算得准 (见 `dots`). */
const LIMIT = 60;

/** 那一排切换器的字. 和 Rail 上那一行一样是版式: 后端发的是每一期的内容, 不是这三种刊叫什么. */
const PAPER_TABS: { kind: FeedKind; label: string }[] = [
  { kind: "day", label: "日报" },
  { kind: "week", label: "周报" },
  { kind: "month", label: "月报" },
];

export function DailyView({ kind, onKind, onOpenStory }: {
  kind: FeedKind;
  onKind: (kind: FeedKind) => void;
  onOpenStory: (id: string) => void;
}) {
  const list = useFeedEditions(kind, LIMIT);
  // 选中哪一期. 没选过就是最新的一期 —— 首屏要直接有东西可画.
  const [picked, setPicked] = useState<string | null>(null);
  const rows = list.data ?? [];
  const currentId = picked ?? rows[0]?.id;
  const issue = useFeedEdition(currentId);
  const groups = archiveGroups(kind, rows, LIMIT);
  // 报眼那点点阵只有在这一组期数完整时才数得准, 所以"完整"要从分组那边问.
  const whole = groups.find((group) => group.rows.some((row) => row.id === currentId))?.complete ?? false;

  // 两栏, 不是一整块: 这一页的右栏那一格 (点开的那一件事) 由 FeedView 排在第三列, 版式那
  // 一套 (`.feed-body.daily-body` 的三栏) 归它管. 所以这里返回的是一对兄弟.
  return (
    <>
      <div className="feed-col daily-arch">
        <div className="daily-tabs" role="group" aria-label="刊期">
          {PAPER_TABS.map((tab) => (
            <button
              key={tab.kind}
              type="button"
              className={tab.kind === kind ? "on" : ""}
              onClick={() => onKind(tab.kind)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {list.error ? <div className="note-bar bad">{errorText(list.error)}</div> : null}
        {list.isLoading ? (
          <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读往期。</div>
        ) : null}
        {!list.isLoading && !list.error && rows.length === 0 ? (
          <div className="feed-empty">还没有出过刊 —— 一期都没有, 不是读不到。</div>
        ) : null}
        {groups.map((group) => (
          <section className="daily-group" key={group.label}>
            <div className="daily-group-head">
              <b>{group.label}</b>
              {group.complete ? <span>{group.rows.length} 期</span> : null}
            </div>
            <ul className="daily-list">
              {group.rows.map((row) => (
                <li key={row.id}>
                  <button
                    type="button"
                    className={"daily-row" + (row.id === currentId ? " on" : "")}
                    onClick={() => setPicked(row.id)}
                  >
                    <span className="daily-mark">{markOf(kind, row.key)}</span>
                    <span className="daily-text">
                      <span className="daily-head">{headlineOf(row)}</span>
                      {metaOf(kind, row) ? (
                        <span className="daily-meta">{metaOf(kind, row)}</span>
                      ) : null}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>

      <aside className="feed-pane daily-pane">
        <div className="pane-scroll">
          {issue.error ? <div className="note-bar bad">{errorText(issue.error)}</div> : null}
          {issue.isLoading && currentId ? (
            <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读这一期。</div>
          ) : null}
          {issue.data ? (
            <Paper issue={issue.data} kind={kind} rows={rows} whole={whole} onPick={setPicked}
                   onOpenStory={onOpenStory} />
          ) : null}
        </div>
      </aside>
    </>
  );
}

/** 谁在说, 以及怎么说. `sources` 是独立来源数 —— 同一家转载九次算一家, 所以它和"有几条"
 *  是两个数. 悬停时列出来的那几名是**哪几家**, 一期存的原始记录就在手边, 不列白不列.
 *
 *  `certainty_label` 是判词的来源: 两家独立来源报到的是事实, 只有一家在说的是讨论, 后端判的,
 *  页面照印 (CLAUDE.md 2.3.3). */
function Who({ entry }: { entry: FeedEditionEntry }) {
  const judgement = entry.judgement;
  const parties = entry.discoveries.map((record) => record.source_id).join("、");
  return (
    <div className="paper-who">
      {judgement ? (
        <span className={"paper-cert " + judgement.certainty}>{judgement.certainty_label}</span>
      ) : null}
      <span title={parties || undefined}>{entry.sources} 家独立来源</span>
      <span>最后一条 {clockText(entry.last_seen_at)}</span>
    </div>
  );
}

/** 摘要和标题一字不差时就不印它. 详情页也有这条规矩 (DESIGN.md 4.9): 同一句话印两遍, 读者会
 *  以为第二遍要说点别的. 规则版的摘要是原文第一句, 标题常常就是那一句, 所以这不是特例. */
function repeats(summary: string | undefined, title: string): boolean {
  return !summary || summary.trim() === title.trim();
}

function Paper({ issue, kind, rows, whole, onPick, onOpenStory }: {
  issue: FeedEdition;
  kind: FeedKind;
  rows: Issue[];
  whole: boolean;
  onPick: (id: string) => void;
  onOpenStory: (id: string) => void;
}) {
  const entries = issue.entries;
  const head = masthead(kind, issue);
  const matrix = dots(kind, issue, rows);
  const at = rows.findIndex((row) => row.id === issue.id);
  // 相邻的两期以**后端给的**为准 (`prev`/`next`): 左栏一次只读 60 期, 翻到边界时列表里没
  // 有下一期, 而后端知道还有. 列表里正好有那一行就用它印出日子, 没有就只印"上一期".
  // 列表是新的在前, 所以 `prev` 更旧 —— 左按钮往回翻, 右按钮往新翻.
  const olderId = issue.prev || (at >= 0 ? rows[at + 1]?.id ?? "" : "");
  const newerId = issue.next || (at >= 0 ? rows[at - 1]?.id ?? "" : "");
  const older = rows.find((row) => row.id === olderId);
  const newer = rows.find((row) => row.id === newerId);
  const kinds = new Map<string, number>();
  for (const entry of entries) {
    kinds.set(entry.kind_label, (kinds.get(entry.kind_label) ?? 0) + 1);
  }
  const [lead, ...rest] = entries;
  // 一期的四块互斥, 合起来正好是整期 —— 分法是后端给的 (`highlights` / `flashes`):
  //   头条 (名次第一) / 要点 (判词是「事实」档) / 看点 (其余展开的) / 快讯 (只够一行的).
  // 各印各的, 同一件就不会在纸上出现两遍: 一句话印两遍, 读者会以为第二遍要说点别的.
  const pins = issue.highlights.filter((row) => row.identity_key !== lead?.identity_key);
  const pinKeys = new Set(issue.highlights.map((row) => row.identity_key));
  const flashes = issue.flashes.filter((row) => row.identity_key !== lead?.identity_key);
  const flashKeys = new Set(issue.flashes.map((row) => row.identity_key));
  const body = rest.filter((row) => !pinKeys.has(row.identity_key)
    && !flashKeys.has(row.identity_key));

  return (
    <article className="paper">
      {/* 报头是"刊头 + 报眼 + 指标条"一整块, 所以它们在一个 header 里: 中间那条粗线以下才是
          版面, 而指标条贴着那条线的上沿 —— 它是报头的一部分, 不是第一条新闻. */}
      <header className="paper-head">
        <div className="paper-mast">
          <div className="paper-names">
            <span className="paper-big">{head.mark}</span>
            <span className="paper-period">
              <b>{head.line1}</b>
              <span>{head.line2}</span>
            </span>
          </div>
          <div className="paper-eye">
            <span className="paper-issue">第 {issue.issue_number} 期</span>
            <div className="paper-dots"
                 style={{ gridTemplateColumns: `repeat(${matrix.columns}, 9px)` }}>
              {Array.from({ length: matrix.first }, (_, index) => (
                <i className="pad" key={"pad-" + index} />
              ))}
              {matrix.cells.map((cell) => (
                <i className={cell.on ? "on" : ""} key={cell.label} title={cell.label} />
              ))}
            </div>
            {whole ? <span className="paper-dots-cap">{matrix.caption}</span> : null}
          </div>
        </div>
        {issue.metrics.length > 0 ? (
          <div className="paper-metrics">
            {issue.metrics.map((metric) => (
              <span key={metric.key}><b>{metric.value}</b> {metric.label}</span>
            ))}
          </div>
        ) : null}
      </header>

      {/* 导语 (5.7): 模型写的一段话, 讲整期在说什么. 没配模型时后端给空,
          这一块就不印 —— 不留一个空框子。 */}
      {issue.lead ? <p className="paper-intro">{issue.lead}</p> : null}

      {!lead ? (
        <p className="paper-blank">
          这一期读了 {issue.seen} 条, 没有够格的. 空的一期也是一句话, 不是出错。
        </p>
      ) : null}

      {lead ? (
        <section className="paper-lead">
          <span className="it-cat">{lead.kind_label}</span>
          {/* 纸上的字点开的是我们自己的事件页, 不是原文: 一期是一段时期的定稿, 而那一件事
              现在什么样, 详情页算得出来 (原文还在事件页底下那个「打开原文」). */}
          <button type="button" className="paper-h"
                  onClick={() => onOpenStory(lead.identity_key)}>{lead.title}</button>
          {repeats(lead.judgement?.summary, lead.title) ? null : (
            <p className="paper-sum">{lead.judgement?.summary}</p>
          )}
          {lead.judgement?.why ? <p className="paper-why">{lead.judgement.why}</p> : null}
          <Who entry={lead} />
        </section>
      ) : null}

      {pins.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">要点</div>
          <ul className="paper-pins">
            {pins.map((row) => (
              <li key={row.identity_key}>
                <span className="paper-pin">要点</span>
                <button type="button" className="paper-title"
                        onClick={() => onOpenStory(row.identity_key)}>{row.title}</button>
                {row.summary ? <p className="paper-one">{row.summary}</p> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {body.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">今日看点</div>
          <ol className="paper-list">
            {body.map((entry, index) => (
              <li key={entry.identity_key}>
                {/* 编号从 1 起: 头条是头条, 不是"看点 01" —— 从 2 开始数会把头条也算进看点里. */}
                <span className="paper-no">{String(index + 1).padStart(2, "0")}</span>
                <div className="paper-item">
                  <button type="button" className="paper-title"
                          onClick={() => onOpenStory(entry.identity_key)}>{entry.title}</button>
                  {repeats(entry.judgement?.summary, entry.title) ? null : (
                    <p className="paper-one">{entry.judgement?.summary}</p>
                  )}
                  <Who entry={entry} />
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {flashes.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">快讯</div>
          <ul className="paper-flashes">
            {flashes.map((row) => (
              <li key={row.identity_key}>
                <span className="paper-flash-time">{clockText(row.last_seen_at)}</span>
                <button type="button" className="paper-flash-title"
                        onClick={() => onOpenStory(row.identity_key)}>{row.title}</button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {kinds.size > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">本期版面</div>
          <ul className="paper-kinds">
            {[...kinds].map(([label, count]) => (
              <li key={label}>
                <span className="paper-kind">{label}</span>
                <b>{count}</b>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {olderId || newerId ? (
        <footer className="paper-next">
          {olderId ? (
            <button type="button" className="btn sec" onClick={() => onPick(olderId)}>
              <Icon as={ChevronLeft} size={14} />
              上一期{older ? " · " + periodText(kind, older) : ""}
            </button>
          ) : <span />}
          {newerId ? (
            <button type="button" className="btn sec" onClick={() => onPick(newerId)}>
              下一期{newer ? " · " + periodText(kind, newer) : ""}
              <Icon as={ChevronRight} size={14} />
            </button>
          ) : null}
        </footer>
      ) : null}
    </article>
  );
}
