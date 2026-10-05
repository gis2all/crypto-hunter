import { ExternalLink, LoaderCircle } from "lucide-react";

import { errorText } from "../../api/client";
import { useFeedStory, useFeedTaxonomy } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { DisplayTags, Sparkline, Trend, word } from "./bits";

/** 一件事的整页, 印在列表右边那一格里.
 *
 *  热点榜上那张卡回答"这事有多热", 这一页回答"这件事是什么": 它为什么热 / 规则版综述 / 最新
 *  进展 / 报道时间线. 综述与时间线都来自同一份 `event.items`, 后端一次算出来, 所以两处不可能
 *  对不上 —— 页面也不自己再排一次 (2.3.4).
 *
 *  时间线上每一行点得进去: 一件事的读法有两种, 先看这件事的轮廓, 或者直接读某一家写的那条.
 */

export function StoryPage({ id, onOpenItem, onOpenStory, onOpenTopic }: {
  id: string;
  onOpenItem: (id: string) => void;
  onOpenStory: (id: string) => void;
  onOpenTopic: (name: string) => void;
}) {
  const query = useFeedStory(id);
  const bar = useFeedTaxonomy();
  const labels = bar.data?.labels;

  if (query.isLoading) {
    return <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读这件事。</div>;
  }
  if (query.error) return <div className="note-bar bad">{errorText(query.error)}</div>;
  const story = query.data;
  if (!story) return null;

  return (
    <article className="dt">
      <div className="dt-tags">
        {story.rank > 0 ? (
          <span className="it-flag">{word(labels, "rank")} {story.rank}</span>
        ) : null}
        {story.badges.map((badge) => <span className="it-cat" key={badge}>{badge}</span>)}
        <span className="it-cat">{story.category_label}</span>
        <DisplayTags tags={story.display_tags} fallback={story.tags} limit={3}
                    onOpenTopic={onOpenTopic} />
      </div>

      <h1 className="dt-title">{story.title}</h1>

      <div className="dt-meta">
        <span>{word(labels, "first_seen")} {story.first_seen_ago}</span>
        <span>{word(labels, "last_seen")} {story.last_seen_ago}</span>
        <span>{word(labels, "report_sources")} <b>{story.source_count}</b></span>
      </div>

      <section className="dt-sec">
        <div className="dt-h">{word(labels, "why_hot")}</div>
        <div className="dt-hot">
          <div className="dt-hot-num">
            <span>{word(labels, "heat")}</span>
            <b>{Math.round(story.heat_index)}</b>
          </div>
          {/* 详情页画 7 天那条曲线 (snapshot_heat.series); 快照还没攒够时退回卡片那条
              24 小时的线. */}
          <Sparkline points={story.heat.length > 0 ? story.heat.map((point) => point.heat) : story.series}
                     width={220} height={38} />
          <Trend trend={story.trend} pct={story.trend_pct} labels={labels} />
        </div>
        <div className="dt-hot-more">
          <span>{word(labels, "recent_sources")} <b>{story.recent_sources}</b></span>
          <span>{word(labels, "recent_reports")} <b>{story.recent_reports}</b></span>
        </div>
      </section>

      {story.summary.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">事件概览</div>
          {story.summary.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
        </section>
      ) : null}

      {/* 当事方自己发的那些 (AIHOT 的 `officialReports`): 和「报道时间线」里别家的说法分开
          印, 读者一眼看得出哪些话是项目方自己说的. 空就不画 —— 没有官方声音不是一条消息. */}
      {story.official_reports.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">官方报道</div>
          <ul className="fu">
            {story.official_reports.map((row) => (
              <li key={row.id}>
                <span className="fu-src">{row.source_name}</span>
                <button type="button" className="fu-title" onClick={() => onOpenItem(row.id)}>
                  {row.title}
                </button>
                <span className="fu-time">{row.time_label}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {story.latest ? (
        <section className="dt-sec">
          <div className="dt-h">{word(labels, "latest")}</div>
          <p>{story.latest}</p>
        </section>
      ) : null}

      {story.days.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">报道时间线</div>
          {story.days.map((day) => (
            <div className="dt-day" key={day.date}>
              <div className="feed-day-head">
                <b>{day.date_label}</b>
                <span>{day.weekday_label}</span>
                <span className="feed-day-count">{day.count} 条</span>
              </div>
              <ul className="dt-tl">
                {day.items.map((row) => (
                  <li key={row.id}>
                    <span className="dt-tl-time">{row.time_label}</span>
                    <button type="button" className="dt-tl-title" onClick={() => onOpenItem(row.id)}>
                      {row.title}
                    </button>
                    <span className="dt-tl-src">{row.source_name}</span>
                    {row.folded > 0 ? (
                      <span className="dt-tl-fold">+{row.folded}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      ) : null}

      {story.related.length > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">关联事件</div>
          <ul className="dt-rel">
            {story.related.map((ref) => (
              <li key={ref.id}>
                <span className="dt-rel-kind">{ref.relation_label}</span>
                <button type="button" className="dt-tl-title" onClick={() => onOpenStory(ref.id)}>
                  {ref.title}
                </button>
                <span className="dt-tl-src">{ref.last_seen_ago}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {story.entities.length > 0 ? (
        <div className="dt-ents">
          {story.entities.map((name) => (
            <button type="button" className="dt-ent" key={name}
                    onClick={() => onOpenTopic(name)}>{name}</button>
          ))}
        </div>
      ) : null}

      <a className="btn dt-open" href={story.url} target="_blank" rel="noreferrer">
        <Icon as={ExternalLink} size={14} />
        {word(labels, "open_original")}
      </a>
    </article>
  );
}
