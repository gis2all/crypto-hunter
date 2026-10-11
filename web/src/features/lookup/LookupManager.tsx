import { Trash2, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";

import { errorText } from "../../api/client";
import type { Favorite } from "../../api/client";
import { useClearLookups, useForgetLookup } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { chainName, stamp } from "../report/foot";
import { onlyRecords, rowKey, shortCa } from "./rows";

/** 搜索管理: the records 最近搜索 is holding, and the two writes that drop them.
 *
 *  最近搜索 grows by itself - every address ever read leaves a card behind, which is what
 *  makes a second look at one free - so a page that could only add to it would be a
 *  list the reader cannot tidy. This drawer is that tidy-up, and it is a drawer for the same
 *  reason 配置管理 is one: it edits the list itself while the page below reads the list.
 *
 *  A delete drops the record **and the card that came with it**, which is what "I never
 *  looked this up" means. A row the reader starred is not only a record - it is a card on
 *  the shelf - so those rows are greyed out with the reason on the button instead of being
 *  offered a press that the backend would refuse: the star is the only write that ends a
 *  collection, and this drawer is not a second one (ConfigManager 对 默认 / 生效
 *  同样处理). 清空 says how many it would take before it takes them.
 */

export function LookupManager({
  rows,
  kept,
  labels,
  open,
  onClose,
}: {
  rows: Favorite[];
  /** The (chain, CA) pairs on the shelf, off `useFavorites` - the one place the shelf is
   *  written, so the star a row is drawn from and the star it is guarded by cannot
   *  disagree. */
  kept: Set<string>;
  labels: Map<string, string>;
  open: boolean;
  onClose: () => void;
}) {
  const drop = useForgetLookup();
  const clear = useClearLookups();
  const [armed, setArmed] = useState(false);

  // Closing and reopening must not come back armed: that confirmation was for the list as it
  // stood, and the list is a moving thing - the reader may have searched while it was shut.
  useEffect(() => {
    if (!open) {
      setArmed(false);
    }
  }, [open]);

  // Exactly what one more press would take - the number on the button is the size of the
  // thing, so it is counted the same way the write is keyed.
  const free = onlyRecords(rows, kept).length;
  const failure = drop.error ?? clear.error;

  return (
    <aside className={`panel wide${open ? " on" : ""}`} id="panel-lookups">
      <div className="panel-h">
        <b>搜索管理</b>
        <button className="x" onClick={onClose} aria-label="关闭" title="关闭">
          <Icon as={X} size={14} />
        </button>
      </div>
      <div className="panel-b">
        <div className="grp">
          <div className="grp-h">最近搜索</div>
          {rows.length ? rows.map((row) => {
            const onShelf = kept.has(rowKey(row));
            return (
              <div className="frow" key={rowKey(row)}>
                <b className="cfg-name">{row.symbol ?? "—"}</b>
                <span className="lk-meta">
                  {shortCa(row.ca)} · {chainName(labels, row.chain_id)} · {stamp(row.origin_label)}
                </span>
                {onShelf ? <em className="cfg-tag on">已收藏</em> : null}
                <span className="cfg-acts">
                  <button
                    type="button"
                    className="btn sec cfg-btn"
                    disabled={onShelf || drop.isPending}
                    title={onShelf ? "已收藏，先取消收藏" : "删除这条记录"}
                    onClick={() => drop.mutate({ chainId: row.chain_id, ca: row.ca })}
                  >
                    <Icon as={Trash2} size={12} />
                    删除
                  </button>
                </span>
              </div>
            );
          }) : (
            <div className="lk-empty">还没有搜索过任何 CA。</div>
          )}
          <div className="frow cfg-foot">
            <button
              type="button"
              className={`btn sec lk-clear${armed ? " arm" : ""}`}
              disabled={!free || clear.isPending}
              title={free ? "只清空没有收藏的记录" : "没有可清空的记录"}
              onClick={() => {
                if (armed) {
                  clear.mutate();
                  setArmed(false);
                  return;
                }
                setArmed(true);
              }}
            >
              {clear.isPending ? "清空中…" : armed ? `确认清空 ${free} 条` : "清空"}
            </button>
          </div>
        </div>

        {failure ? (
          <div className="note-bar bad">
            <Icon as={TriangleAlert} size={14} />
            <span>{errorText(failure)}</span>
          </div>
        ) : null}
      </div>
    </aside>
  );
}
