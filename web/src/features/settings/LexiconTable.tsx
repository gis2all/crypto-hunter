import type { LexiconRow } from "../../api/client";
import { CopyCa } from "../../components/CopyCa";
import { NoteHelp } from "./NoteHelp";

function meter(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return <span className="na-v">—</span>;
  }
  const pct = Math.max(0, Math.min(1, value / 5)) * 100;
  return (
    <span className="qmeter">
      <span className="meter">
        <i style={{ width: `${pct.toFixed(0)}%` }} />
      </span>
      <span className="qnum">{value.toFixed(1)}</span>
    </span>
  );
}

/** What the deep pass calls each state. The backend decides the state; this only names it. */
const RESEARCH: Record<string, string> = {
  none: "未跑",
  queued: "排队",
  running: "进行中",
  done: "已重档",
  failed: "失败",
};

function age(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) {
    return "写于 时间未知";
  }
  if (hours < 1) {
    return "写于 不到 1 小时前";
  }
  if (hours < 48) {
    return `写于 ${hours.toFixed(0)} 小时前`;
  }
  return `写于 ${(hours / 24).toFixed(1)} 天前`;
}

/**
 * The freshness line under an entry. The archive is 45% of the score and a whole card of
 * prose, so "how old is this" is not a detail: `stale` is the backend answering with the same
 * rule the round uses, and the badge only prints it.
 */
function freshness(row: LexiconRow): string {
  const state = row.stale ? "待重写" : "在有效期内";
  const research = RESEARCH[row.research_state] ?? row.research_state;
  return `${age(row.age_hours)} · ${state} · 重档 ${research}`;
}

/** The two lines that used to print beside the title - what the lexicon is, and how many
 *  entries it holds - read once and then stayed in the way on every later visit, so they
 *  ride in the same popover as the backend's table rules. Order is top-down: what it is,
 *  how big it is, then how to read a row. */
function helpText(note: string, count: number): string {
  const head = `赛道产品分的来源 · 属于这条链\n整库 ${count} 条`;
  // An empty `note` would otherwise leave a blank line in the popover.
  return note ? `${head}\n${note}` : head;
}

/** The lexicon belongs to this chain: the track score reads straight off it.
 *
 *  How to read the table ships as `note` from the backend - the weight it quotes is the
 *  same one the block header prints, and no rule about a field is written twice. */
export function LexiconTable({ rows, note }: { rows: LexiconRow[]; note: string }) {
  return (
    <details className="pgroup lex-group">
      <summary className="pgroup-h">
        <b>词条库</b>
        {/* A click inside a <summary> toggles the <details>, so the icon's own click has
            to decline that: opening the reading rules must not fold the table away. */}
        <span className="lex-help" onClick={(event) => event.preventDefault()}>
          <NoteHelp text={helpText(note, rows.length)} />
        </span>
      </summary>
      <table className="tbl lex-tbl">
        <thead>
        <tr>
          <th className="txt">CA</th>
          <th className="txt">标的</th>
          <th>市值</th>
          <th className="txt">词条（叙事）</th>
          <th>叙事贴合</th>
          <th>产品完整度</th>
          <th className="txt">本轮</th>
        </tr>
        </thead>
        <tbody>
        {rows.map((row) => {
          const extra = (row.sources ?? []).length - 2;
          return (
            <tr key={row.ca} className={row.current ? "cur" : undefined}>
              <td className="txt ca-cell">
                <span className="ca-wrap">
                  <span className="ca-v">{row.ca}</span>
                  <CopyCa ca={row.ca} />
                </span>
              </td>
              <td className="txt">
                {row.symbol ?? "—"}
                {(row.sources ?? []).length > 0 ? (
                  <span className="lex-src">
                    {(row.sources ?? []).slice(0, 2).map((source) => (
                      <a key={source.url} href={source.url} target="_blank" rel="noreferrer">
                        {source.label}
                      </a>
                    ))}
                    {extra > 0 ? `+${extra}` : null}
                  </span>
                ) : null}
              </td>
              <td className="mcap" title="最近一次扫描看到的市值">
                {row.mcap}
              </td>
              <td className="txt">
                {row.tag ?? "—"}
                <span
                  className={`lex-badge${row.stale ? " stale" : ""}`}
                  title={row.refresh_reason ?? undefined}
                >
                  {freshness(row)}
                </span>
              </td>
              <td>{meter(row.narrative)}</td>
              <td>{meter(row.product)}</td>
              <td className="txt">{row.current ? <span className="cur-mark">本轮</span> : null}</td>
            </tr>
          );
        })}
        </tbody>
      </table>
    </details>
  );
}
