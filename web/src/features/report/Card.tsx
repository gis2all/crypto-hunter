import { Boxes, ChevronRight, ExternalLink, ShieldCheck } from "lucide-react";
import { Fragment, useLayoutEffect, useRef, useState } from "react";

import type { Card as CardModel } from "../../api/client";
import { CopyCa } from "../../components/CopyCa";
import { Icon } from "../../components/Icon";
import { Rich } from "../../components/Rich";

function one(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(1);
}

/** The 结论, five lines of it, with the rest one hover away.
 *
 * The box is a fixed height so the cards are: every block on a collapsed card is fixed
 * except this one, and a round whose cards each end somewhere else cannot be read down a
 * column. Five lines is the height that fits twelve cards - see the `--verdict-lines`
 * block in app.css, which is also where the number can be turned.
 *
 * The popover is built only when the text is really cut. `scrollHeight` still reports the
 * full box under `-webkit-line-clamp`, so the measurement is exact rather than a guess at
 * a character count, and a conclusion that fits does not open a panel repeating what is
 * already on the page. Hover and focus open it; a click does not, so dragging across a
 * conclusion to copy it stays an ordinary selection.
 *
 * 全部展开 reaches this too (2026-09-26): a clamped conclusion under a bulk-open card is
 * the one block still asking for a hover, so the bulk state drops the clamp as well (CSS,
 * `.verdict-wrap.full`) and there is nothing left to reveal. 全部收起 puts both back. */
function Verdict({ segments, full }: { segments: CardModel["verdict"]; full: boolean }) {
  const box = useRef<HTMLParagraphElement>(null);
  const [cut, setCut] = useState(false);
  const [open, setOpen] = useState(false);

  // 全部展开 is the reader saying "show me everything", and a clamped conclusion under it
  // is the one block still asking for a hover. The clamp itself is in CSS; what changes
  // here is that the popover is not built and the box is not a tab stop while the text is
  // all on the page. `full` re-runs the measurement, so both directions stay exact.
  const clipped = cut && !full;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    // A resize can re-wrap the text and put the last line back inside the box, so the
    // answer is re-derived rather than remembered from the first layout.
    const measure = () => setCut(el.scrollHeight > el.clientHeight + 1);
    measure();
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, [segments, full]);

  return (
    <div className={"verdict-wrap" + (full ? " full" : "") + (open && clipped ? " open" : "")}>
      <p
        className="verdict"
        ref={box}
        tabIndex={clipped ? 0 : -1}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        <Rich segments={segments} />
      </p>
      {clipped ? (
        <div className="verdict-pop" role="note">
          <Rich segments={segments} />
        </div>
      ) : null}
    </div>
  );
}

function rank(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `#${value}`;
}

/** `openAll` + `bulkTick` are the topbar's 全部展开 / 全部收起 reaching every card at
 *  once. The two <details> are uncontrolled - the reader owns them - so a bulk action
 *  remounts them under a new key rather than trying to become their state. One click
 *  then lands on all five cards, and a manual toggle keeps working until the next one. */
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
  // The chain-detail tables are optional in the schema: a card whose feed answered
  // nothing has no table to draw, and an empty one would be a row of dashes.
  const holders = card.safety.holders;
  const pools = card.safety.pools;
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
          <span className="score">{one(card.score)}</span>
          <span className="score-label">综合分</span>
        </div>
      </header>

      <div className="scorebar">
        <div className="sb">
          <div className="sb-seg" style={{ flex: `${card.onchain_weight} 1 0` }}>
            <div className="sb-fill" style={{ width: `${card.onchain ?? 0}%` }} />
          </div>
          <div className="sb-seg" style={{ flex: `${card.product_weight} 1 0` }}>
            <div className="sb-fill alt" style={{ width: `${card.product ?? 0}%` }} />
          </div>
        </div>
        <div className="sb-lab">
          <span>
            <Icon as={ShieldCheck} />
            <b>链上质量</b>
            <i>{one(card.onchain)}</i>
            <em>×{card.onchain_weight}%</em>
          </span>
          <span className="r">
            <Icon as={Boxes} />
            <b>赛道产品</b>
            <i>{one(card.product)}</i>
            <em>×{card.product_weight}%</em>
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
          {/* The words and the numbers come from two different times. When the entry was
              not written this round, the card says so, and says why - the alternative is a
              verdict quoting a market cap four days old beside a table showing today. */}
          {card.entry_note ? <em className="age-mark">{card.entry_note}</em> : null}
        </h4>
        <Verdict segments={card.verdict} full={openAll} />
      </section>

      <details key={`quality-${bulkTick}`} className="card-sec sec-quality" open={openAll}>
        <summary className="sec-sum">
          <Icon as={ChevronRight} />
          <span className="sec-h">链上质量</span>
          <span className="sec-hint">质量表 / 安全 / 筹码 / 池子</span>
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
            {holders && holders.rows.length > 0 ? (
              <div className="srow">
                <span className="srow-k">筹码分布</span>
                <span className="srow-v hblock">
                  <span className="hsum chips">
                    {holders.summary.map((chip) => (
                      <span key={chip.label} className={`chip chip-${chip.tone}`}>
                        <b>{chip.label}</b>
                        {chip.value}
                      </span>
                    ))}
                    {holders.note ? (
                      <span className="hnote">{holders.note}</span>
                    ) : null}
                  </span>
                  <table className="htab">
                    <tbody>
                      {holders.rows.map((row) => (
                        <tr key={row.addr_full || row.rank}>
                          <td className="hrank">{row.rank}</td>
                          <td className="haddr" title={row.addr_full}>
                            {row.addr}
                          </td>
                          <td className="htags">{row.tags.join(" · ")}</td>
                          <td className="hpct">{row.pct}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </span>
              </div>
            ) : null}
            {pools && pools.rows.length > 0 ? (
              <div className="srow">
                <span className="srow-k">池子清单</span>
                <span className="srow-v hblock">
                  <table className="ptab">
                    <tbody>
                      {pools.rows.map((pool) => (
                        <tr key={pool.pair_full || pool.name}>
                          <td className="pname" title={pool.name}>
                            {pool.name}
                          </td>
                          <td className="ppair" title={pool.pair_full}>
                            {pool.pair}
                          </td>
                          <td className="pliq">{pool.liquidity}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {pools.note ? (
                    <span className="hnote">{pools.note}</span>
                  ) : null}
                </span>
              </div>
            ) : null}
          </div>
        </div>
      </details>

      <details key={`detail-${bulkTick}`} className="card-sec sec-detail" open={openAll}>
        <summary className="det-sum">
          <Icon as={ChevronRight} />
          展开详述 <span>产品 / 机制 / 社媒 / 优势 / 风险 / 点评</span>
        </summary>
        {card.details.map((detail) => (
          <section key={detail.title} className="dsec">
            <h4 className="dsec-h">{detail.title}</h4>
            <div className="dsec-b">
              {detail.variant === "note" ? (
                <p className="note">
                  <Rich segments={detail.body} />
                </p>
              ) : (
                <Rich segments={detail.body} />
              )}
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
              <Icon as={ExternalLink} />
            </a>
          </Fragment>
        ))}
      </footer>
    </article>
  );
}
