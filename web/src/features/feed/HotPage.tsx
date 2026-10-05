import { LoaderCircle } from "lucide-react";

import type { FeedStory } from "../../api/client";
import { errorText } from "../../api/client";
import { useFeedHot, useFeedTaxonomy } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { Faces } from "./Faces";
import { HotBadge, Sparkline, Trend, word, type Labels } from "./bits";

/** 热点榜: 不是"哪几条新", 而是"哪几件事正在被多方同时说".
 *
 *  名次是后端的: 热度由"有几家独立来源在说"折上时间算出来, 那个数页面看不到, 也就不该自己排
 *  (CLAUDE.md 2.3.4). 这一页只做一件事 —— 把名次画出来: No.01 一张大卡, No.02/03 两张并排,
 *  No.04 往后一行一条.
 *
 *  一张卡上左边的字回答"发生了什么", 右边的数回答"有多热". 两块都要: 只有标题, 读者不知道
 *  这事值不值得点; 只有数字, 读者不知道点开是看什么.
 *
 *  标题通向这件事的整页 (DESIGN.md 4.8): 它是这一页上最像入口的东西, 就该是入口. 那几枚来源小牌子
 *  仍然是外链 —— 它们的问法是"这家说了什么", 那是另一件事.
 */

/** 从第几名开始收成一行一条. No.01 独占一张, No.02/03 并排, 其余进列表. */
const REST_FROM = 4;

export function HotPage({ onOpenStory }: { onOpenStory: (id: string) => void }) {
  const bar = useFeedTaxonomy();
  const hot = useFeedHot(10);
  const labels = bar.data?.labels;
  const stories = hot.data?.stories ?? [];

  if (hot.error) {
    return (
      <div className="feed">
        <div className="note-bar bad">{errorText(hot.error)}</div>
      </div>
    );
  }
  if (hot.isLoading) {
    return (
      <div className="feed">
        <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读这一批。</div>
      </div>
    );
  }
  if (stories.length === 0) {
    return (
      <div className="feed">
        <div className="feed-empty">{word(labels, "empty_hot")}</div>
      </div>
    );
  }

  const [lead, ...rest] = stories;
  const pair = rest.slice(0, REST_FROM - 2);
  const tail = rest.slice(REST_FROM - 2);

  return (
    <div className="feed">
      <HotCard story={lead} rank={1} labels={labels} onOpenStory={onOpenStory} />
      {pair.length > 0 ? (
        <div className="hot-pair">
          {pair.map((story, index) => (
            <HotCard key={story.id} story={story} rank={index + 2} labels={labels}
                     onOpenStory={onOpenStory} />
          ))}
        </div>
      ) : null}
      {tail.length > 0 ? (
        <>
          <div className="hot-sec">继续看</div>
          <ol className="hot-list">
            {tail.map((story, index) => (
              <HotRow key={story.id} story={story} rank={index + REST_FROM} labels={labels}
                      onOpenStory={onOpenStory} />
            ))}
          </ol>
        </>
      ) : null}
    </div>
  );
}

/** 前几名的一张卡. 左边的序号是名次本身, 不是"第几条": 热点榜上名次就是内容. */
function HotCard({ story, rank, labels, onOpenStory }: {
  story: FeedStory;
  rank: number;
  labels: Labels | undefined;
  onOpenStory: (id: string) => void;
}) {
  return (
    <article className={"hot-card" + (rank === 1 ? " lead" : "")}>
      <div className="hot-rank">
        <b>{String(rank).padStart(2, "0")}</b>
        <span>排名</span>
      </div>
      <div className="hot-main">
        <div className="hot-tags">
          <span className="hot-cat">{story.category_label}</span>
          {story.tags.slice(0, 2).map((tag) => <span className="hot-tag" key={tag}>{tag}</span>)}
          <span className="hot-srcs">{word(labels, "source_count")} {story.source_count}</span>
          <Faces faces={story.faces} size={20} />
        </div>
        <h2 className="hot-title">
          <HotBadge badges={story.hot_badges} labels={labels} />
          <button type="button" onClick={() => onOpenStory(story.id)}>{story.title}</button>
        </h2>
        {story.latest ? (
          <p className="hot-latest">
            <span>{word(labels, "latest")}</span>
            {story.latest}
          </p>
        ) : null}
        <div className="hot-src">
          {story.items.slice(0, 4).map((item) => (
            <a key={item.url} href={item.url} target="_blank" rel="noreferrer"
               className={item.first_party ? "on" : undefined}>
              {item.source_name}
              <i>{item.time_label}</i>
            </a>
          ))}
        </div>
      </div>
      <div className="hot-num">
        <div className="hot-heat">
          <span>{word(labels, "heat")}</span>
          <b>{Math.round(story.heat_index)}</b>
        </div>
        <Sparkline points={story.series} />
        <Trend trend={story.trend} pct={story.trend_pct} labels={labels} />
      </div>
    </article>
  );
}

/** 第 4 名往后的一行. 一行里只留三样: 名次, 标题, 热度. 到了这一档, 读者是在扫, 不是在读. */
function HotRow({ story, rank, labels, onOpenStory }: {
  story: FeedStory;
  rank: number;
  labels: Labels | undefined;
  onOpenStory: (id: string) => void;
}) {
  return (
    <li className="hot-row">
      <span className="hot-row-no">{String(rank).padStart(2, "0")}</span>
      <div className="hot-row-body">
        <span className="hot-row-head">
          <HotBadge badges={story.hot_badges} labels={labels} />
          <button type="button" className="hot-row-title" onClick={() => onOpenStory(story.id)}>
            {story.title}
          </button>
        </span>
        <div className="hot-row-meta">
          <span>{story.category_label}</span>
          <span>{word(labels, "source_count")} {story.source_count}</span>
          <Faces faces={story.faces} size={16} max={4} />
          <Trend trend={story.trend} pct={story.trend_pct} labels={labels} />
        </div>
      </div>
      <b className="hot-row-heat">{Math.round(story.heat_index)}</b>
    </li>
  );
}