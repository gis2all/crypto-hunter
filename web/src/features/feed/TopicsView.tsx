import { LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { errorText } from "../../api/client";
import { useFeedTaxonomy, useFeedTopicItems, useFeedTopics } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { word } from "./bits";

/** 主题: 按"这条东西牵了谁"看, 而不是按"这条东西是什么"看.
 *
 *  和全部动态/精选的那条筛选栏不是一回事. 那边的类别是**互斥**的 —— 一条东西恰好属于一个类别,
 *  所以它能回答"这是哪一类"; 这里的实体是**交叉**的 —— 一条可以牵好几个 (也可以一个都不牵),
 *  所以它能回答"这条线今天动了没有". 一份报告和一个追踪对象, 两个问题, 两份词表.
 *
 *  这一页是三栏, 但只有前两栏在这个文件里: 第三栏 (点开的那一条) 和三个实时视图的右栏是同一个
 *  组件 (FeedView.tsx 的 `Pane`), 所以返回 / Esc / 压层的规矩一处都不用重写. 版式上它跟着
 *  报刊走 (`.feed-body.topics-body`), 因为两者是同一个形状: 左边一栏目录, 中间一栏内容,
 *  右边一栏点开的详情. 差别在谁最宽 —— 纸要版面, 正文要宽度, 所以那 3fr 在这两页落在不同的列上.
 *
 *  左栏的条数与中栏那一批是**同一个窗口、同一次读** (都走 views.TOPIC_HOURS), 所以中栏顶上
 *  那个数直接用后端回的 `shown`, 不自己数一遍 —— 页面数出来的数和后端给的数, 迟早会差一条.
 *
 *  窗口的长短 (30 天) 不在这个文件里: 后端发 `window_days`, 这里照印.
 *
 *  选中哪一条线是这个组件自己的状态, 和报刊选中哪一期一样: 从这一页切走再回来, 选中的那一条
 *  跟着组件一起重来. 空线 (窗口里一条都没有) 画出来但是点不动 —— 藏掉的话, 读者不知道这条线
 *  存在, 只是现在没有东西.
 */

export function TopicsView({ onOpenItem, initialTopic }: {
  onOpenItem: (id: string) => void;
  /** 从卡片上一个实体角标点进来时, 直接落在这一条线上. */
  initialTopic?: string;
}) {
  const dir = useFeedTopics();
  const bar = useFeedTaxonomy();
  const labels = bar.data?.labels;
  const groups = dir.data?.groups ?? [];
  const rows = groups.flatMap((group) => group.rows);
  // 没选过就是第一条有东西的线: 首屏要直接有东西可画, 而一条空线画不出东西.
  const [picked, setPicked] = useState<string | null>(initialTopic ?? null);
  // 已经在这一页上时, 再点另一个实体角标也要跟着换.
  useEffect(() => {
    if (initialTopic) setPicked(initialTopic);
  }, [initialTopic]);
  const topic = picked ?? rows.find((row) => row.count > 0)?.id ?? "";
  const list = useFeedTopicItems(topic);
  const days = list.data?.days ?? [];

  return (
    <>
      <div className="feed-col topics-arch">
        {dir.data ? (
          <div className="topics-note">
            按实体 · 近 <b>{dir.data.window_days}</b> 天
          </div>
        ) : null}
        {dir.error ? <div className="note-bar bad">{errorText(dir.error)}</div> : null}
        {dir.isLoading ? (
          <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读名录。</div>
        ) : null}
        {groups.map((group) => (
          <section className="topics-group" key={group.key}>
            <div className="topics-group-head"><b>{group.name}</b></div>
            <ul className="topics-list">
              {group.rows.map((row) => (
                <li key={row.id}>
                  <button
                    type="button"
                    className={"topic-row" + (row.id === topic ? " on" : "")}
                    disabled={row.count === 0 && row.id !== topic}
                    onClick={() => setPicked(row.id)}
                  >
                    <span className="topic-name">{row.name}</span>
                    <span className="topic-count">{row.count}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>

      <aside className="feed-pane">
        {topic ? (
          <div className="topics-head">
            <b>{topic}</b>
            <span>
              近 {list.data?.window_days ?? dir.data?.window_days ?? 0} 天 · {list.data?.shown ?? 0} 条
            </span>
          </div>
        ) : null}
        <div className="pane-scroll">
          {list.error ? <div className="note-bar bad">{errorText(list.error)}</div> : null}
          {list.isLoading ? (
            <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读这条线。</div>
          ) : null}
          {!list.isLoading && !list.error && days.length === 0 ? (
            <div className="feed-empty">{word(labels, topic ? "empty_topic" : "empty_all")}</div>
          ) : null}
          {days.map((day) => (
            <section className="line-day" key={day.date}>
              <div className="line-day-head">
                <b>{day.date_label}</b>
                <span>{day.weekday_label}</span>
                <span className="line-day-count">{day.count} 条</span>
              </div>
              <ul className="line-list">
                {day.items.map((row) => (
                  <li key={row.id}>
                    <button type="button" className="line-row" onClick={() => onOpenItem(row.id)}>
                      <span className="line-time">{row.time_label}</span>
                      <span className="line-body">
                        <span className="line-title">{row.title}</span>
                        <span className="line-meta">
                          {row.source_name}
                          {row.category_label ? " · " + row.category_label : ""}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </aside>
    </>
  );
}
