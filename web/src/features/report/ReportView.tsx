import { TriangleAlert } from "lucide-react";

import { errorText } from "../../api/client";
import type { Batch, Card as CardModel } from "../../api/client";
import { Icon } from "../../components/Icon";
import { BatchHead } from "./BatchHead";
import { Card } from "./Card";

/** The round: the batch strip (round, funnel, what entered and left) and the cards side
 *  by side. Never a horizontal scroll - the grid reflows instead. */
export function ReportView({
  batch,
  loading,
  error,
  canPrev,
  canNext,
  onPrev,
  onNext,
  openAll,
  bulkTick,
  favorited,
  onFavorite,
}: {
  batch: Batch | undefined;
  loading: boolean;
  error: unknown;
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
  openAll: boolean;
  bulkTick: number;
  /** The CAs the shelf already holds. A report page only asks - the shelf is another view. */
  favorited: Set<string>;
  onFavorite: (card: CardModel) => void;
}) {
  if (error) {
    return (
      <div id="view-report">
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>报告读取失败：{errorText(error)}</span>
        </div>
      </div>
    );
  }
  if (!batch) {
    return (
      <div id="view-report">
        <div className="note-bar">
          {loading ? "读取中…" : "还没有跑过一轮。到「设定」里点「立即运行」。"}
        </div>
      </div>
    );
  }
  return (
    <div id="view-report">
      <BatchHead
        batch={batch}
        canPrev={canPrev}
        canNext={canNext}
        onPrev={onPrev}
        onNext={onNext}
      />
      <div className="grid">
        {batch.cards.length ? (
          batch.cards.map((card) => (
            <Card
              key={card.ca}
              card={card}
              openAll={openAll}
              bulkTick={bulkTick}
              favorited={favorited.has(card.ca)}
              onFavorite={() => onFavorite(card)}
            />
          ))
        ) : (
          <div className="note-bar">
            这一轮没有写出报告卡。{batch.note ? ` ${batch.note}` : ""}
          </div>
        )}
      </div>
    </div>
  );
}
