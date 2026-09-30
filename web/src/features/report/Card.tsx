import { Boxes, ChevronDown, ChevronRight, ShieldCheck } from "lucide-react";
import { Fragment, useLayoutEffect, useRef, useState } from "react";

import type { Card as CardModel } from "../../api/client";
import { CopyCa } from "../../components/CopyCa";
import { Icon } from "../../components/Icon";
import { Rich } from "../../components/Rich";
import { whole } from "../../format";

/** The 结论, ten lines of it, with the rest one click away.
 *
 * The box is a fixed height so the cards are: every block on a collapsed card is fixed
 * except this one, and a round whose cards each end somewhere else cannot be read down a
 * column. Ten lines is the height that fits twelve cards - see the `--verdict-lines`
 * block in app.css, which is also where the number can be turned.
 *
 * The text that does not fit is opened by the 展开 button in the bottom-right corner of the
 * box, and put back by the same button reading 收起 (2026-09-30, user). It used to be a
 * hover that floated the whole conclusion over the card below: it could not be pinned open,
 * and it covered the card it landed on.
 *
 * The button is built only when the text is really cut. `scrollHeight` still reports the
 * full box under `-webkit-line-clamp`, so the measurement is exact rather than a guess at a
 * character count, and a conclusion that fits opens nothing.
 *
 * 展开卡片 reaches this too (2026-09-26): a clamped conclusion under a bulk-open card is
 * the one block still asking for a click, so the bulk state drops the clamp as well (CSS,
 * `.verdict-wrap.full`) and there is nothing left to reveal. 收起卡片 puts both back. */
function Verdict({ segments, full }: { segments: CardModel["verdict"]; full: boolean }) {
  const box = useRef<HTMLParagraphElement>(null);
  const [cut, setCut] = useState(false);
  const [open, setOpen] = useState(false);

  // 展开卡片 and this card own 展开 drop the clamp, and a box without a clamp always
  // reports "not cut" - so the measurement is skipped while either is on and the answer
  // stands from the layout that had the clamp. Measuring there is a loop: `cut` clears, the
  // button goes away, the clamp comes back, the text is cut again, and the button blinks at
  // every resize tick (seen on 2026-09-30).
  const expanded = full || (open && cut);
  const grown = useRef(expanded);
  grown.current = expanded;

  // The button exists only while there is something to open: 展开卡片 has already put the
  // whole conclusion on the page, and an uncut one never had anything hidden.
  const more = cut && !full;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    // A resize can re-wrap the text and put the last line back inside the box, so the
    // answer is re-derived rather than remembered from the first layout.
    const measure = () => {
      if (grown.current) return;
      setCut(el.scrollHeight > el.clientHeight + 1);
    };
    measure();
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, [segments, full]);

  return (
    <div className={"verdict-wrap" + (expanded ? " full" : "")}>
      <p className="verdict" ref={box}>
        <Rich segments={segments} />
      </p>
      {more ? (
        <div className="verdict-more">
          <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "收起" : "展开"}
            <Icon as={ChevronDown} size={14} />
          </button>
        </div>
      ) : null}
    </div>
  );
}

function rank(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `#${value}`;
}

/** `openAll` + `bulkTick` are the topbar's 展开卡片 / 收起卡片 reaching every card at
 *  once. The two <details> and the 展开 button under the conclusion are uncontrolled - the
 *  reader owns them - so a bulk action remounts them under a new key rather than trying to
 *  become their state. One click then lands on all five cards, and a manual toggle keeps
 *  working until the next one: 收起卡片 puts back the ten lines and the 展开 button on the
 *  card the reader had opened too, which is what one round of the bulk control means. */
export function Card({
  card,
  openAll,
  bulkTick,
}: {
  card: CardModel;
  openAll: boolean;
  bulkTick: number;
}) {
  const short = `${card.ca.slice(0, 6)}…${card.ca.slice(-4)}`;
  // Ranks 1-3 carry the podium strip; the colours live in app.css.
  const podium = card.rank >= 1 && card.rank <= 3 ? " podium-" + card.rank : "";
  // The size of the thing, beside the shape of its price: the two numbers a reader checks
  // before opening a card. Read off the table the card already carries rather than sent a
  // second time, so the line above and the row below it cannot disagree.
  const cap = card.metrics.find((metric) => metric.key === "市值");
  return (
    <article className={"card" + podium}>
      <header className="card-head">
        <div className="head-l">
          <span className="rank">{String(card.rank).padStart(2, "0")}</span>
          <span className="sym">{card.sym}</span>
          <span className={card.is_new ? "flag flag-new" : "flag"}>
            {card.is_new ? "新进" : "续榜"}
          </span>
        </div>
        <div className="head-r">
          <span className="score">{whole(card.score)}</span>
          <span className="score-label">综合分</span>
        </div>
      </header>

      <div className="scorebar">
        <div className="sb">
          <div className="sb-seg" style={{ flex: `${card.onchain_weight} 1 0` }}>
            <div className="sb-fill" style={{ width: `${card.onchain ?? 0}%` }} />
          </div>
          <div className="sb-seg" style={{ flex: `${card.offchain_weight} 1 0` }}>
            <div className="sb-fill alt" style={{ width: `${card.product ?? 0}%` }} />
          </div>
        </div>
        <div className="sb-lab">
          <span>
            <Icon as={ShieldCheck} />
            <b>链上质量</b>
            <i>{whole(card.onchain)}</i>
          </span>
          <span className="r">
            <Icon as={Boxes} />
            <b>链下调研</b>
            <i>{whole(card.product)}</i>
          </span>
        </div>
      </div>

      <div className="card-id">
        <code className="ca">{short}</code>
        <CopyCa ca={card.ca} />
        <span className="ranks">
          流动性 {rank(card.ranks.liquidity)} · 成交 {rank(card.ranks.volume)} · 持有人{" "}
          {rank(card.ranks.holders)}
        </span>
      </div>

      <div className="card-spark">
        <svg
          viewBox="0 0 128 26"
          preserveAspectRatio="none"
          className={`trend-${card.spark.trend}`}
        >
          <path d={card.spark.path} />
        </svg>
        <span className="spark-label">6d {card.spark.delta}</span>
        {cap ? (
          <span className="spark-cap">
            市值 <b>{cap.value}</b>
          </span>
        ) : null}
      </div>

      <section className="card-sec sec-verdict">
        <h4 className="sec-h">
          结论
        </h4>
        <Verdict key={`verdict-${bulkTick}`} segments={card.verdict} full={openAll} />
      </section>

      <details key={`quality-${bulkTick}`} className="card-sec sec-quality" open={openAll}>
        <summary className="sec-sum">
          <Icon as={ChevronRight} />
          <span className="sec-h">链上质量</span>
        </summary>
        <div className="sec-body">
          <div className="kv-grid">
            {card.metrics.map((metric) => (
              <div
                key={metric.key}
                className={`kv${metric.na ? " na" : ""}${metric.long ? " long" : ""}`}
              >
                <span className="kv-k">{metric.key}</span>
                <span
                  className={`kv-v${metric.tone ? ` hb hb-${metric.tone}` : ""}`}
                  title={metric.title}
                >
                  {metric.value}
                </span>
              </div>
            ))}
          </div>
          {card.deltas.length > 0 ? (
            <div className="deltas">
              {card.deltas.map((delta, index) => (
                <span
                  key={`${index}-${delta.key}`}
                  className={`delta ${(delta.value ?? "").trim().startsWith("-") ? "neg" : "flat"}`}
                >
                  <i>{delta.key}</i>
                  {delta.value}
                </span>
              ))}
            </div>
          ) : null}
          <div className="safety">
            <div className="srow">
              <span className="srow-k">合约权限</span>
              <span className="srow-v">
                {card.safety.perms.map((pill) => (
                  <span key={pill.label} className={`pill pill-${pill.tone}`}>
                    {pill.label}
                  </span>
                ))}
              </span>
            </div>
            <div className="srow">
              <span className="srow-k">LP 锁定</span>
              <span className="srow-v">
                <span className={`pill pill-${card.safety.lp.tone}`}>
                  {card.safety.lp.label}
                </span>
              </span>
            </div>
            <div className="srow">
              <span className="srow-k">筹码与团队</span>
              <span className="srow-v chips">
                {card.safety.chips.map((chip) => (
                  <span key={chip.label} className={`chip chip-${chip.tone}`}>
                    <b>{chip.label}</b>
                    {chip.value}
                  </span>
                ))}
              </span>
            </div>
            {card.safety.holdings.length > 0 ? (
              <div className="srow">
                <span className="srow-k">团队持仓</span>
                <span className="srow-v chips">
                  {card.safety.holdings.map((chip) => (
                    <span key={chip.label} className={`chip chip-${chip.tone}`}>
                      <b>{chip.label}</b>
                      {chip.value}
                    </span>
                  ))}
                </span>
              </div>
            ) : null}
            {card.safety.warnings.length > 0 ? (
              <div className="srow">
                <span className="srow-k">附加警示</span>
                <span className="srow-v">
                  {card.safety.warnings.map((pill) => (
                    <span key={pill.label} className={`pill pill-${pill.tone}`}>
                      {pill.label}
                    </span>
                  ))}
                </span>
              </div>
            ) : null}
          </div>
        </div>
      </details>

      <details key={`detail-${bulkTick}`} className="card-sec sec-detail" open={openAll}>
        <summary className="det-sum">
          <Icon as={ChevronRight} />
          链下调研
        </summary>
        {card.details.map((detail) => (
          <section key={detail.title} className="dsec">
            <h4 className="dsec-h">
              {detail.title}
              {detail.badge ? <span className="dsec-badge">{detail.badge}</span> : null}
            </h4>
            <div className="dsec-b">
              <Rich segments={detail.body} />
            </div>
          </section>
        ))}
      </details>

      <footer className="card-foot">
        {card.links.map((link, index) => (
          <Fragment key={link.url}>
            {index > 0 ? " ｜ " : null}
            <a href={link.url} target="_blank" rel="noreferrer">
              {link.label}
            </a>
          </Fragment>
        ))}
      </footer>
    </article>
  );
}
