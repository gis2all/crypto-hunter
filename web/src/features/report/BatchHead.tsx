import { ChevronLeft, ChevronRight } from "lucide-react";

import type { Batch, ChangeOut } from "../../api/client";
import { Icon } from "../../components/Icon";

/** The round's own line: which round (with its steppers), the funnel that produced it,
 *  and what entered and left. 本期变化 used to be a second full-width bar under this
 *  one, drawing a 40px stripe for eight chips that are a reading of the same round -
 *  so it sits in the row it belongs to, after the funnel, and the strip is one line. */
export function BatchHead({
  batch,
  canPrev,
  canNext,
  onPrev,
  onNext,
}: {
  batch: Batch;
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  const { funnel } = batch;
  const changes: ChangeOut[] = batch.changes;
  // A payload written before the round started counting these has neither; the strip is
  // still worth drawing, so the note simply does not appear.
  const gate = batch.gate ?? { judged: 0, unjudged: 0 };
  return (
    <div className="bhead">
      <div className="batch">
        <button
          className={`step${canPrev ? "" : " demo"}`}
          onClick={onPrev}
          disabled={!canPrev}
          title="上一轮"
        >
          <Icon as={ChevronLeft} />
        </button>
        <b>{batch.batch_label || "未命名批次"}</b>
        <button
          className={`step${canNext ? "" : " demo"}`}
          onClick={onNext}
          disabled={!canNext}
          title="下一轮"
        >
          <Icon as={ChevronRight} />
        </button>
      </div>
      <div className="funnel">
        <span>扫描</span>
        <b>{funnel.scanned.toLocaleString()}</b>
        <i>→</i>
        <span>淘汰</span>
        <b>{funnel.dropped.toLocaleString()}</b>
        <i>→</i>
        <span>候选</span>
        <b>{funnel.candidates.toLocaleString()}</b>
        <i>→</i>
        <span>入选</span>
        <b>{funnel.selected.toLocaleString()}</b>
        {gate.unjudged > 0 && (
          <em className="fnote">
            已判定 {gate.judged} · 未判定 {gate.unjudged}
          </em>
        )}
      </div>
      {/* Eight chips are a reading, not a section: kept in the row, clipped rather
          than allowed to widen it - the one thing this layout must never do. */}
      {changes.length ? (
        <div className="changes">
          <span className="lbl">本期变化</span>
          {changes.map((change, index) => (
            <span
              key={`${index}-${change.sym}`}
              className={change.direction === "in" ? "chg in" : "chg out"}
              title={change.detail}
            >
              {change.kind} {change.sym}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
