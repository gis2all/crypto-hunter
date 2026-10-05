import { ExternalLink, LoaderCircle } from "lucide-react";
import { useState } from "react";

import { errorText } from "../../api/client";
import { useFeedItem, useFeedTaxonomy } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { DisplayTags, word } from "./bits";

/** 点目录滚到那一节. 锚点是后端注在正文里的 (`outline[].id`), 所以这里只按 id 找 —— 页面不认
 *  标题文字, 也不自己给正文排一次序. */
function scrollToSection(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** 一条报道的整页, 印在列表右边那一格里.

 *  版式照 AIHOT 的详情页: 中文标题, 底下灰字是源自己那条标题; 摘要那一栏 (模型写过的就叫
 *  「AI 导读」); 然后才是正文, 正文那一栏自己带语言开关. 没有副标题这一格 —— 它和摘要说同一
 *  件事 (同一句话印两遍, 读者会以为那是两件事); 也没有页面底部的原文折叠块, 原文就在正文那一栏
 *  里, 那个开关就是它的去处.
 *
 *  语言开关只在**真有译文**时给 (`body_zh`): 中文源的条目没有可切的那一份, 给它一个切过去没有
 *  变化的按钮, 比不给更坏. 没有译文时正文只有一份, 栏名也就不带后缀.
 *
 *  正文是后端净化过的块级 HTML (htmlbody.py): 块是源自己的段落与小标题, 行内标签在入库时就压成了
 *  文字. 所以这里直接印 —— 净化在**入库那一侧**做完, 页面不猜哪个标签可信.
 *
 *  窗口里没有的东西就不画: 没有别家报道就没有那一行, 事件里只有它自己那一条也没有"所属事件" (DESIGN.md 4.9).
 */

export function ItemPage({ id, onOpenStory, onOpenItem, onOpenTopic }: {
  id: string;
  onOpenStory: (id: string) => void;
  onOpenItem: (id: string) => void;
  onOpenTopic: (name: string) => void;
}) {
  const query = useFeedItem(id);
  const bar = useFeedTaxonomy();
  const labels = bar.data?.labels;
  // 看译文还是看原文. 默认译文 —— 这个产品的读者读中文, 而原文那一份没有消失, 只是收起来了.
  const [source, setSource] = useState(false);

  if (query.isLoading) {
    return <div className="feed-empty"><Icon as={LoaderCircle} size={14} />正在读这一条。</div>;
  }
  if (query.error) return <div className="note-bar bad">{errorText(query.error)}</div>;
  const item = query.data;
  if (!item) return null;

  const translated = Boolean(item.body_zh);
  // 没有译文就没有"切"这回事: 这个开关只在一个方向上有意义.
  const original = source || !translated;
  const html = original ? item.body_html : item.body_zh;
  const bodyLabel = !translated ? word(labels, "body")
    : original ? word(labels, "body_src") : word(labels, "body_zh");

  return (
    <article className="dt">
      <div className="dt-tags">
        <span className="it-src">{item.source_name}</span>
        {item.first_party ? <span className="it-flag">{word(labels, "first_party")}</span> : null}
        <span className="it-cat">{item.category_label}</span>
        <DisplayTags tags={item.display_tags} fallback={item.tags} limit={3}
                    onOpenTopic={onOpenTopic} />
      </div>

      <div className="dt-head">
        <h1 className="dt-title">{item.title}</h1>
        {item.title_original
          ? <p className="dt-orig-title">{item.title_original}</p>
          : null}
      </div>

      <div className="dt-meta">
        <span>{item.published_label}</span>
        <span>{item.published_ago}</span>
        {item.author ? <span>{item.author}</span> : null}
        {item.domain ? <span>{item.domain}</span> : null}
        <span className="dt-score">
          {word(labels, "score")} <b>{item.score}</b>
        </span>
      </div>

      {/* 「AI 导读」这个说法只在字**真的是模型写的**时才是真话: 写作站关掉的那一档里, 摘要取的是
          正文第一段, 把它印在一栏叫「AI 导读」的地方就是一句谎话. 两个说法都由后端给
          (views.py 的 LABELS), 判据是列表行上的 `written`. */}
      {item.summary ? (
        <section className="dt-sec">
          <div className="dt-h">{word(labels, item.written ? "summary_ai" : "summary")}</div>
          <p className="dt-lead">{item.summary}</p>
        </section>
      ) : null}

      {/* 引用块 (3.6): 别人的原话, 所以它有自己的一栏, 不混进正文. 译文在上面、原文在下面 ——
          这个产品的读者读中文, 而那句话毕竟不是我们写的, 原文要留着给人对. */}
      {item.x_view?.quoted?.text ? (
        <section className="dt-sec">
          <div className="dt-h">{word(labels, "quote")}</div>
          <blockquote className="dt-quote">
            {item.x_view.quoted.handle
              ? <cite>@{item.x_view.quoted.handle}</cite>
              : null}
            {item.x_view.quoted.translation
              ? <p className="dt-quote-zh">{item.x_view.quoted.translation}</p>
              : null}
            <p className="dt-quote-src">{item.x_view.quoted.text}</p>
          </blockquote>
        </section>
      ) : null}

      {html ? (
        <section className="dt-sec">
          <div className="dt-body-h">
            <span className="dt-h">{bodyLabel}</span>
            {translated ? (
              <div className="dt-lang" role="group">
                <button type="button" className={original ? "" : "on"}
                        onClick={() => setSource(false)}>{word(labels, "translated")}</button>
                <button type="button" className={original ? "on" : ""}
                        onClick={() => setSource(true)}>{word(labels, "original")}</button>
              </div>
            ) : null}
          </div>
          {/* 半篇也是答案, 但读者要知道这不是整篇 —— 缺块的提示只在看译文时给: 切到原文, 那一份
              本来就是完整的. */}
          {translated && !original && !item.body_complete ? (
            <p className="dt-note">{word(labels, "body_incomplete")}</p>
          ) : null}
          {/* 正文目录 (AIHOT 的 `outline`): 长文才给. 少于两条时后端就不发, 这里再判一次是
              因为空数组在页面上会留一个空框子. */}
          {item.outline.length > 1 ? (
            <nav className="dt-outline" aria-label="正文目录">
              {item.outline.map((entry) => (
                <button key={entry.id} type="button" className={"lv" + entry.level}
                        onClick={() => scrollToSection(entry.id)}>
                  {entry.text}
                </button>
              ))}
            </nav>
          ) : null}
          <div className="dt-body" dangerouslySetInnerHTML={{ __html: html }} />
        </section>
      ) : null}

      {item.followup_count > 0 ? (
        <section className="dt-sec">
          <div className="dt-h">事件后续 <em>· {item.followup_count}</em></div>
          <ul className="fu">
            {item.followups.map((row) => (
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

      {item.entities.length > 0 ? (
        <div className="dt-ents">
          {item.entities.map((name) => (
            <button type="button" className="dt-ent" key={name}
                    onClick={() => onOpenTopic(name)}>{name}</button>
          ))}
        </div>
      ) : null}

      {item.story_title && item.story_id ? (
        <button type="button" className="dt-story" onClick={() => onOpenStory(item.story_id)}>
          <span>{word(labels, "story_of")}</span>
          <b>{item.story_title}</b>
        </button>
      ) : null}

      <a className="btn dt-open" href={item.url} target="_blank" rel="noreferrer">
        <Icon as={ExternalLink} size={14} />
        {word(labels, "open_original")}
      </a>
    </article>
  );
}
