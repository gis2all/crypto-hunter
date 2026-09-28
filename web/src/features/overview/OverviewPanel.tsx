import { X } from "lucide-react";
import { Fragment } from "react";

import type { Batch } from "../../api/client";
import { CopyCa } from "../../components/CopyCa";
import { Icon } from "../../components/Icon";
import { whole } from "../../format";

/** 本轮总览: the round's口径, its changes, who missed the cut and why, and push state. */
export function OverviewPanel({
  batch,
  open,
  onClose,
}: {
  batch: Batch | undefined;
  open: boolean;
  onClose: () => void;
}) {
  if (!batch) {
    return null;
  }
  return (
    <aside className={`panel wide${open ? " on" : ""}`} id="panel-overview">
      <div className="panel-h">
        <b>本轮总览 · {batch.batch_label}</b>
        <button className="x" onClick={onClose} aria-label="关闭" title="关闭">
          <Icon as={X} size={14} />
        </button>
      </div>
      <div className="panel-b">
        <div className="grp">
          <div className="grp-h">本期口径（由设定生成，单一来源）</div>
          <div className="frow stack">
            <span className="hint">
              {batch.caliber.length
                ? batch.caliber.map((line, index) => (
                    <Fragment key={index}>
                      {index > 0 ? <br /> : null}
                      {line}
                    </Fragment>
                  ))
                : "这一轮没有记录口径"}
            </span>
          </div>
        </div>

        <div className="grp">
          <div className="grp-h">本期变化</div>
          {batch.changes.length ? (
            batch.changes.map((change, index) => (
              <div className="frow stack" key={`${index}-${change.sym}`}>
                <label>{change.kind}</label>
                <span className="hint">{change.detail}</span>
              </div>
            ))
          ) : (
            <div className="frow">
              <span className="hint">本轮没有新进或退出</span>
            </div>
          )}
        </div>

        <div className="grp">
          <div className="grp-h">
            {/* 候选 is the pool the gate drew from and 入选 are inside it, so the table
                below is the difference and the two numbers agree with the strip. */}
            落选池 · 候选 {batch.funnel.candidates} → 未入选 {batch.decline.length}
          </div>
          <div className="decline">
            <table className="tbl">
              <thead>
              <tr>
                <th>币</th>
                <th className="txt">CA</th>
                <th>市值</th>
                <th>流动性</th>
                <th>持有人</th>
                <th>陷阱</th>
                <th>评分</th>
                <th>落选原因</th>
              </tr>
              </thead>
              <tbody>
              {batch.decline.map((row) => (
                <tr key={row.sym}>
                  <td>{row.sym}</td>
                  <td className="txt">{row.ca ? <CopyCa ca={row.ca} /> : "—"}</td>
                  <td>{row.market_cap}</td>
                  <td>{row.liquidity}</td>
                  <td>{row.holders}</td>
                  <td>{row.entrapment}</td>
                  <td>{whole(row.score)}</td>
                  <td>{row.reason}</td>
                </tr>
              ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="grp">
          <div className="grp-h">推送</div>
          <div className="frow">
            <label>状态</label>
            <span className="val">{batch.push.status || "—"}</span>
          </div>
          <div className="frow">
            <label>延迟</label>
            <span className="val">{batch.push.delay || "—"}</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
