import { Boxes, ChevronDown, ChevronRight, RotateCw, ShieldCheck, Star } from "lucide-react";
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import type { Card as CardModel } from "../../api/client";
import { CopyCa } from "../../components/CopyCa";
import { Icon } from "../../components/Icon";
import { Logo } from "../../components/Logo";
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
  favorited = false,
  onFavorite,
  onRescan,
  rescanBusy = false,
  foot,
  listed = card.rank >= 1,
}: {
  card: CardModel;
  openAll: boolean;
  bulkTick: number;
  /** Already on the shelf. The star is drawn filled and clicking it takes the card off. */
  favorited?: boolean;
  /** Keep this card, or take it off. Absent where there is no shelf to act on. */
  onFavorite?: () => void;
  /** Run this same address again from scratch, replacing the card. Only 搜索 passes one:
   *  a card that came out of a round is already that round's answer, and has nothing to run
   *  again. */
  onRescan?: () => void;
  /** A search for this card is in flight, so the control is spent until it lands. */
  rescanBusy?: boolean;
  /** A second footer band, under the links. Only the shelf passes one: it is where a kept
   *  card says which round and which set of settings it was read as, and a card drawn from
   *  the round it is standing in has nothing to say there. */
  foot?: ReactNode;
  /** Whether this card is standing in a round's list right now. It defaults to what the card
   *  itself says (`rank`), and 最近搜索 is the one page that answers no: a searched token the
   *  next round picked up carries that round's rank, score and flag in its payload (10.10),
   *  while that page is not the round's list - see the note on rank below. */
  listed?: boolean;
}) {
  const short = `${card.ca.slice(0, 6)}…${card.ca.slice(-4)}`;
  // Ranks 1-3 carry the podium strip; the colours live in app.css.
  const podium = listed && card.rank >= 1 && card.rank <= 3 ? " podium-" + card.rank : "";
  /** 取消收藏要按第二下（2026-10-29, user）：第一下只把星标置为待确认，星旁出现「取消
   *  收藏」，五秒内点它才真的摘掉。和新机会卡片同一个做法，两条线学一次就够。
   *  文字放在星与分数之间：分数始终贴着卡片右边缘，只有星标向左让位。 */
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) {
      return;
    }
    const timer = window.setTimeout(() => setArmed(false), 5000);
    return () => window.clearTimeout(timer);
  }, [armed]);
  // 星标一旦落定（收进来或被摘掉）、或者这张卡换成了另一个币，待确认就作废。
  useEffect(() => { setArmed(false); }, [favorited, card.ca]);
  // A card with no rank is not a card that came last - it is a card no round put in a list at
  // all, which is what a CA search produces. Rank is where that shows: the ordinal, the 新进 /
  // 续榜 flag (there was no previous round to be 续 of) and the three pool ranks all read off
  // it, so the head is the card's own name and nothing else. The score block reads off the
  // same rank, for the same reason: a score is a position inside a round's pool, so a card
  // with no pool has no score to print - not a blank one waiting to be explained, and no two
  // halves to split a bar between.
  // That rank is what `listed` defaults to. A caller that knows this card is not standing in
  // any list says so itself, and then none of it is drawn.
  const ranked = listed && [card.ranks.liquidity, card.ranks.volume, card.ranks.holders]
    .some((value) => value !== null && value !== undefined);
  // The size of the thing, beside the shape of its price: the two numbers a reader checks
  // before opening a card. Read off the table the card already carries rather than sent a
  // second time, so the line above and the row below it cannot disagree.
  const cap = card.metrics.find((metric) => metric.key === "市值");
  return (
    <article className={"card" + podium}>
      <header className="card-head">
        <div className="head-l">
          <Logo url={card.logo} />
          <div className="head-id">
            <div className="head-name">
              <span className="sym">{card.sym}</span>
              {listed ? (
                <span className={card.is_new ? "flag flag-new" : "flag"}>
                  {card.is_new ? "新进" : "续榜"}
                </span>
              ) : null}
            </div>
            <div className="card-id">
              <code className="ca">{short}</code>
              <CopyCa ca={card.ca} />
            </div>
          </div>
        </div>
        <div className="head-r">
          {/* 重新搜索 is the second control a card can carry, and it is icon-only: the corner
              has room for one glyph beside the star, and a word here would weigh more than the
              card's own name. The word it gives up is the tooltip, and it turns while the
              search it started is in flight - an icon-only control that has gone quiet has
              nothing else to say. */}
          {onRescan ? (
            <button
              type="button"
              className="rescan"
              aria-label="重新搜索"
              title="重新搜索"
              disabled={rescanBusy}
              onClick={onRescan}
            >
              <Icon as={RotateCw} size={14} className={rescanBusy ? "spin" : undefined} />
            </button>
          ) : null}
          {/* The one control on a card that keeps it rather than opens it, drawn filled once
              the shelf holds this CA. It stands in the corner the score would own; on a
              searched card the 重新搜索 glyph is the only thing beside it. */}
          {onFavorite ? (
            <button
              type="button"
              className={"fav" + (favorited ? " on" : "")}
              aria-pressed={favorited}
              aria-label={favorited ? "取消收藏" : "收藏这张卡"}
              title={favorited ? "取消收藏" : "收藏这张卡"}
              onClick={() => {
                if (favorited && !armed) {
                  setArmed(true);
                  return;
                }
                setArmed(false);
                onFavorite();
              }}
            >
              <Icon as={Star} size={14} />
            </button>
          ) : null}
          {onFavorite && armed ? (
            <button
              type="button"
              className="fav-confirm"
              onClick={() => {
                setArmed(false);
                onFavorite();
              }}
            >
              取消收藏
            </button>
          ) : null}
          {/* The rank stands under the score (2026-10-02, user): both are readings of the
              round rather than of the token, so they share the one corner the score owned
              instead of sitting at opposite ends of the head. The stack keeps its two lines
              tight (line-height:1) so 21px + 11.5px stays inside the 40px mark beside it
              and the head does not grow - the shelf stands searched cards and round cards
              in one grid, and a head that grows would move every block under it. */}
          {listed ? (
            <span className="score-stack">
              <span className="score-line">
                <span className="score">{whole(card.score)}</span>
                <span className="score-label">综合分</span>
              </span>
              <span className="score-line">
                <span className="rank">{String(card.rank).padStart(2, "0")}</span>
                <span className="score-label">排名</span>
              </span>
            </span>
          ) : null}
        </div>
      </header>

      {listed ? (
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
      ) : (
        /* A searched card has no score to print, but the shelf mixes it with round
           cards, and this card's own rule is that only one block is variable. The score
           block holds its place so this card's conclusion, its two folds and its footer
           still land on the same line as the round card beside it. */
        <div className="scorebar blank" aria-hidden="true" />
      )}

      {/* The pool's own ranking of this token. It used to share the address row and now has
          that row to itself: the address moved up beside the name, and these three numbers
          are a reading of the round rather than of the token, so they sit under the head.
          A card with no round keeps the row and prints nothing in it - the shelf stands
          searched cards and round cards in one grid, and a row present on one and
          missing on the other would move every block under it. */}
      <div className="card-ranks">
        {ranked ? (
          <span className="ranks">
            流动性 {rank(card.ranks.liquidity)} · 成交 {rank(card.ranks.volume)} · 持有人{" "}
            {rank(card.ranks.holders)}
          </span>
        ) : null}
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

      {/* A round writes a card only when it has words for it, so an empty block never reaches
          the page from there. A searched token is answered with whatever it does have, and
          when that is nothing the block says why rather than disappearing - the 结论 above it
          is then the market and chain reading alone, and the card should not look broken. */}
      {card.details.length || card.research_note ? (
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
          {card.research_note ? (
            <section className="dsec">
              <div className="dsec-b">{card.research_note}</div>
            </section>
          ) : null}
        </details>
      ) : null}

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

      {foot ? <div className="card-src">{foot}</div> : null}
    </article>
  );
}
