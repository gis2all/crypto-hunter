import { ChevronDown, ChevronRight, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { FeedDay, FeedItems, FeedListItem } from "../../api/client";
import { errorText } from "../../api/client";
import { useFeedHot, useFeedItemsPaged, useFeedTaxonomy } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { DisplayTags, OtherReports, word, type Labels } from "./bits";
import { isReload, readSnapshot, restoreAnchor, saveSnapshot, type ListSnapshot } from "./restore";

/** 全部动态 与 精选: 同一批条目的两个问法, 所以是一个页面一个组件而不是两个.
 *
 *  两者只差后端的一个 `scope`, 页面这一层一个字都不判断"哪些够精选" —— 那条线是分数, 分数是
 *  后端的 (CLAUDE.md 2.3.4). 页面多认一个门槛, 就是第二个能改精选口径的地方.
 *
 *  筛选栏的类别与计数都来自 `/feed/taxonomy` 与这次回答, 页面不认识任何一个类别名.
 *
 *  一条是一个**块**而不是一行: 左边时刻, 一条竖线把一天串起来, 右边一块底 —— 同一天里的若干条
 *  读起来是"这一天发生的事", 而不是一张表的若干行. 底用本程序自己的方块 (--surface 加 1px
 *  --border), 不用圆角也不用投影: 全站没有第二个圆角卡片 (tokens.css 那条全局零圆角), 引进来
 *  就这一页跟别处不一样了.
 *
 *  块里那几个小件各有出处, 都不是新判断:
 *    · 分数按档上色 (只上色, 不换数) —— 档位是版式, 谁过线仍然是后端的 score;
 *    · 图是这条来源自己带的 (X 这类才有), 挂在 `.it-media`;
 *    · 折叠行是"同一件事别家怎么说它", 后端按信源算好了 —— 它不是"谁引了谁";
 *    · 读过的一条标题淡下去, 已读记在本地 (read.ts).
 *
 *  换页再回来时, "哪几天折着"与"停在哪一条"从会话里回来 (restore.ts): 它们都是页面自己的
 *  版式状态, 不该因为去别的视图看了一眼就丢掉. 刷新打开的那一次不恢复 —— 那是重新来过.
 */

/** 记在会话里的那一份: 折着的几天. 列表本身由查询缓存拿着, 不重复存一遍. */
interface ListState {
  folded: string[];
}

export function ItemsPage({ scope, category, onCategory, onOpenItem, onOpenStory, onOpenBoard,
                            onOpenTopic, read }: {
  scope: "all" | "curated";
  category: string;
  onCategory: (key: string) => void;
  onOpenItem: (id: string) => void;
  onOpenStory: (id: string) => void;
  onOpenBoard: () => void;
  onOpenTopic: (name: string) => void;
  read: Set<string>;
}) {
  const bar = useFeedTaxonomy();
  const items = useFeedItemsPaged(scope, category);
  const labels = bar.data?.labels;
  const pages = items.data?.pages ?? [];
  const first = pages[0];
  const days = mergeDays(pages);
  // 计数为 0 的类别不画: 一个点进去什么都没有的标签是白给的一次点击. 但当前选中的那个
  // 永远留着 —— 否则筛选栏会在读者点进去的那一刻把入口本身收走.
  const categories = (first?.categories ?? [])
    .filter((entry) => entry.count > 0 || entry.key === category);
  // 折起来的那几天. 这一天是不是折着是页面自己的事 (版式), 所以也不落库、不进链接.
  // 去别的视图看一眼再回来, 它还在原处; 刷新打开的那一次从空开始 (restore.ts).
  const cacheKey = scope + ":" + category;
  const pendingAnchor = useRef<ListSnapshot<ListState> | null>(null);
  const [folded, setFolded] = useState<Set<string>>(() => {
    const snap = isReload() ? null : readSnapshot<ListState>(cacheKey);
    pendingAnchor.current = snap;
    return new Set(snap?.data.folded ?? []);
  });
  const fold = (date: string) => setFolded((prev) => {
    const next = new Set(prev);
    if (next.has(date)) next.delete(date);
    else next.add(date);
    return next;
  });
  const foldedRef = useRef(folded);
  foldedRef.current = folded;
  // 停下来那一刻的位置: 滚动时记, 离开这一页时也记一笔 (换视图的那一刻不会再有滚动事件).
  useEffect(() => {
    let timer: number | undefined;
    const remember = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => saveSnapshot<ListState>(cacheKey, { folded: [...foldedRef.current] }), 400);
    };
    const leave = () => saveSnapshot<ListState>(cacheKey, { folded: [...foldedRef.current] });
    window.addEventListener("scroll", remember, { passive: true });
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("scroll", remember);
      window.removeEventListener("pagehide", leave);
      if (timer !== undefined) window.clearTimeout(timer);
      leave();
    };
  }, [cacheKey]);
  // 快照里那一条要在列表落进 DOM 之后再放回去: 数据还没到就调它, 只会找不到那一条.
  useEffect(() => {
    if (pendingAnchor.current && !items.isLoading && days.length > 0) {
      const snap = pendingAnchor.current;
      pendingAnchor.current = null;
      restoreAnchor(snap.anchor);
    }
  }, [items.isLoading, days.length, cacheKey]);
  // 滚到底再要下一页. 观察的是列表末尾那个哨兵: 它一进视野就取下一页 —— 分页是加载方式,
  // 不是读者要按的一个按钮. 留一屏多的余量, 所以还没到底就开始取, 读的人撞不上空白.
  const sentinel = useRef<HTMLDivElement | null>(null);
  const more = items.hasNextPage;
  const busy = items.isFetchingNextPage;
  const fetchMore = items.fetchNextPage;
  useEffect(() => {
    const node = sentinel.current;
    if (!node || !more || busy) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void fetchMore();
    }, { rootMargin: "600px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [more, busy, fetchMore]);

  return (
    <div className="feed">
      <div className="feed-bar">
        <span className={"chip" + (category === "" ? " on" : "")}
              onClick={() => onCategory("")}>全部</span>
        {categories.map((entry) => (
          <span key={entry.key}
                className={"chip" + (entry.key === category ? " on" : "")}
                onClick={() => onCategory(entry.key)}>
            {entry.label}
            <i>{entry.count}</i>
          </span>
        ))}
        {first ? <span className="feed-bar-note">共 {first.filtered} 条</span> : null}
      </div>
      {items.error ? <div className="note-bar bad">{errorText(items.error)}</div> : null}
      {items.isLoading ? (
        <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读这一批。</div>
      ) : days.length === 0 ? (
        <div className="feed-empty">
          {word(labels, scope === "curated" ? "empty_curated" : "empty_all")}
        </div>
      ) : (
        <>
          {scope === "curated"
            ? <CurrentHot labels={labels} onOpenStory={onOpenStory} onOpenBoard={onOpenBoard} />
            : null}
          {days.map((day) => {
            const shut = folded.has(day.date);
            return (
              <section className={"feed-day" + (shut ? " shut" : "")} key={day.date}>
                <div className="feed-day-head">
                  <b>{day.date_label}</b>
                  <button type="button" className="feed-day-fold"
                          aria-expanded={!shut}
                          aria-label={(shut ? "展开" : "收起") + day.date_label}
                          onClick={() => fold(day.date)}>
                    <Icon as={ChevronDown} size={12} />
                  </button>
                  <span>{day.weekday_label}</span>
                  <span className="feed-day-count">{day.count} 条</span>
                </div>
                {shut ? null : (
                  <ul className="it-list">
                    {day.items.map((row) => (
                      <ItemRow key={row.id} row={row} labels={labels}
                               read={read.has(row.id)} onOpen={onOpenItem}
                               onOpenTopic={onOpenTopic} />
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
          {/* 下一页的哨兵也是那一行的提示: 一直在取、取完就换成空, 读者看不到一个停住的转圈.
              高度留给它 (CSS `.feed-more`), 零高度的目标是观察不到的. */}
          {more ? (
            <div className="feed-more" ref={sentinel}>
              {busy ? <><Icon as={LoaderCircle} size={14} />正在读下一页。</> : null}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

/** 把几页的按天分组并成一份.
 *
 *  一天被 40 条那条线切开时, 下一页会再给一次同一天的分组 —— 直接铺开就把同一天印两遍,
 *  而且两处各只数了一半 (读者会以为丢了条目). 所以按日期合: 条数相加, 条目接在后面,
 *  日期之间的先后照第一次出现的顺序.
 */
function mergeDays(pages: FeedItems[]): FeedDay[] {
  const byDate = new Map<string, FeedDay>();
  const order: string[] = [];
  for (const page of pages) {
    for (const day of page.days) {
      const held = byDate.get(day.date);
      if (!held) {
        byDate.set(day.date, { ...day, items: [...day.items] });
        order.push(day.date);
        continue;
      }
      held.count += day.count;
      held.items.push(...day.items);
    }
  }
  return order.map((date) => byDate.get(date) as FeedDay);
}

/** 精选页顶上的「当前热点」, 取前五.
 *
 *  它和 热点榜 那一页问的是同一个问题, 所以两块地方的名字与数字必然一致 —— 都读同一条
 *  /feed/hot. 这一块只是把前五名抬到列表上面: 让读者先看一眼"今天在说的几件事", 再往下翻单条.
 *  五条都拿不到就直接不画, 不占一块空白.
 *
 *  它是一块底 (和下面每一条同一个方块), 不是一段没有边界的文字: 它是页面上另一类东西 —— 榜单
 *  的头部, 不是今天的一条. 右上角那个「完整榜单」是它和热点榜之间唯一的门, 少了它, 想看第六名
 *  的读者没有地方可去. 名次前三用全站那一套三色 (podium-1/2/3, 报告页名次同一个来源).
 */
function CurrentHot({ labels, onOpenStory, onOpenBoard }: {
  labels: Labels | undefined;
  onOpenStory: (id: string) => void;
  onOpenBoard: () => void;
}) {
  const hot = useFeedHot(5);
  const stories = hot.data?.stories ?? [];
  if (stories.length === 0) return null;
  return (
    <section className="cur-hot">
      <div className="cur-hot-head">
        <span className="hot-sec">当前热点</span>
        <button type="button" className="cur-hot-more" onClick={onOpenBoard}>
          完整榜单<Icon as={ChevronRight} size={12} />
        </button>
      </div>
      <ol className="cur-hot-list">
        {stories.map((story, index) => (
          <li key={story.id}>
            <span className={"cur-hot-no" + (index < 3 ? " cur-hot-p" + (index + 1) : "")}>
              {String(index + 1).padStart(2, "0")}
            </span>
            <button type="button" onClick={() => onOpenStory(story.id)}>{story.title}</button>
            <span className="cur-hot-heat">
              <span>{word(labels, "heat")}</span>
              <b>{Math.round(story.heat_index)}</b>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** 分数按档上色. 只有颜色是页面的, 分数本身还是后端那一个: 80 以上是这批里最硬的, 65 以上
 *  是过了精选线的, 再低就是全部动态里的普通一条 (精选页不会出现第三种, 全部动态会). */
const HI = 80;
const MID = 65;

function tier(score: number): string {
  if (score >= HI) return "s-hi";
  if (score >= MID) return "s-mid";
  return "s-lo";
}

/** 列表里的一条. 左边时刻, 右边分数, 中间是谁说的、说了什么、为什么值得看.
 *
 *  标题通向这一条自己的整页 (DESIGN.md 4.8), 原文在那里面 —— 点一下先看我们把它读成了什么, 想读源
 *  自己的话, 详情页上那一个按钮就在手边. 标题直接跳出去的话, 这一页就只剩"相不相信"这一个
 *  选项了.
 */
function ItemRow({ row, labels, read, onOpen, onOpenTopic }: {
  row: FeedListItem;
  labels: Labels | undefined;
  read: boolean;
  onOpen: (id: string) => void;
  onOpenTopic: (name: string) => void;
}) {
  return (
    <li className={"it-row" + (read ? " read" : "")} data-card-key={row.id}>
      <div className="it-time">{row.time_label}</div>
      <span className="it-rail" aria-hidden="true" />
      <div className="it-card">
        <div className="it-body">
          <div className="it-tags">
            <span className="it-src">{row.source_name}</span>
            {row.first_party ? <span className="it-flag">{word(labels, "first_party")}</span> : null}
            <span className="it-cat">{row.category_label}</span>
            <DisplayTags tags={row.display_tags} fallback={row.tags} limit={2}
                        onOpenTopic={onOpenTopic} />
          </div>
          <button type="button" className="it-title" onClick={() => onOpen(row.id)}>{row.title}</button>
          {row.summary && row.summary !== row.title ? (
            <p className="it-sum">{row.summary}</p>
          ) : null}
          {row.media.length > 0 ? <Media urls={row.media} /> : null}
          {row.other_report_count > 0 ? (
            <OtherReports reports={row.other_reports} count={row.other_report_count}
                          labels={labels} onOpenItem={onOpen} />
          ) : null}
          {row.reason ? (
            <p className="it-why"><span>{word(labels, "reason")}</span>{row.reason}</p>
          ) : null}
        </div>
        <div className="it-score">
          <span>{word(labels, "score")}</span>
          <b className={tier(row.score)}>{row.score}</b>
        </div>
      </div>
    </li>
  );
}

/** 来源自带的图 (X 这类才有), 原样的 url, 不下载也不重绘.
 *
 *  挂掉的那张自己收掉: 一块破图占着位置, 比没有图更糟, 而这张图本来就不是这一条的主要内容 ——
 *  正文在上面已经说完了. 全部收掉时这一块也跟着消失, 不留一个空框. */
function Media({ urls }: { urls: string[] }) {
  const [broken, setBroken] = useState<Set<string>>(() => new Set());
  const shown = urls.filter((url) => !broken.has(url));
  if (shown.length === 0) return null;
  return (
    <div className="it-media">
      {shown.map((url) => (
        <img key={url} src={url} alt="" loading="lazy"
             onError={() => setBroken((prev) => new Set(prev).add(url))} />
      ))}
    </div>
  );
}

