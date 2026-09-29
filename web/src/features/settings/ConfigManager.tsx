import { Copy, Pencil, Plus, Trash2, TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";

import { errorText } from "../../api/client";
import type { Chain, ChainConfig } from "../../api/client";
import {
  useAddChainConfig,
  useChainConfig,
  useDeleteChainConfig,
  useRenameChainConfig,
} from "../../api/hooks";
import { Icon } from "../../components/Icon";

/** 配置管理: the four writes that change *which* sets of settings a chain has.
 *
 *  The settings page edits the values inside one set; this drawer edits the list of sets.
 *  They are two surfaces because they are two decisions. Saving a value is also the write
 *  that makes its set the chain's live one, while adding, renaming and dropping a set
 *  change the chain's shape and never move which one is running.
 *
 *  Deleting is the only one of the four that destroys anything - a whole report library -
 *  so it is the only one that asks for the name to be typed back. The backend refuses the
 *  default entry and the live one; those two buttons are off here rather than letting the
 *  trip be made to be told why, and the title on each says what would have to happen first.
 */

type Pending =
  | { kind: "new" }
  | { kind: "copy"; from: ChainConfig }
  | { kind: "rename"; target: ChainConfig }
  | { kind: "delete"; target: ChainConfig };

/** The name as the delete gate compares it: runs of whitespace folded. The backend folds
 *  the same way, so the two never disagree about what "the same name" means. */
function sameLabel(typed: string, label: string): boolean {
  return typed.trim().split(/\s+/).join(" ") === label;
}

export function ConfigManager({
  chain,
  open,
  onClose,
  onSelect,
}: {
  chain: Chain | undefined;
  open: boolean;
  onClose: () => void;
  /** Opening a set of settings in the page below. A new one is looked at straight away;
   *  the page's own pick is the reader's, so nothing here moves it except that. */
  onSelect: (configId: string) => void;
}) {
  const chainId = chain?.id ?? "";
  const configs = chain?.configs ?? [];
  const [pending, setPending] = useState<Pending | null>(null);
  const [label, setLabel] = useState("");
  const add = useAddChainConfig(chainId);
  const rename = useRenameChainConfig(chainId);
  const remove = useDeleteChainConfig(chainId);
  // Asked for only while a delete is on screen: the size of what is about to go is the one
  // number that confirmation prints, and nothing else in this drawer reads it.
  const detail = useChainConfig(
    chainId,
    pending?.kind === "delete" ? pending.target.id : undefined,
  );

  // A drawer that is closed and opened again must not come back on the form a previous
  // visit was showing: the half-typed name is the wrong name for whatever is picked next.
  useEffect(() => {
    if (!open) {
      setPending(null);
      setLabel("");
    }
  }, [open]);

  const start = (next: Pending, seed = "") => {
    setLabel(seed);
    setPending(next);
  };
  const done = (configId?: string) => {
    setPending(null);
    setLabel("");
    if (configId) {
      onSelect(configId);
    }
  };

  const creating = pending && pending.kind !== "rename" && pending.kind !== "delete" ? pending : null;
  const renaming = pending?.kind === "rename" ? pending : null;
  const deleting = pending?.kind === "delete" ? pending : null;
  const failure = add.error ?? rename.error ?? remove.error;

  return (
    <aside className={`panel wide${open ? " on" : ""}`} id="panel-configs">
      <div className="panel-h">
        <b>配置管理 · {chain?.label ?? chainId}</b>
        <button className="x" onClick={onClose} aria-label="关闭" title="关闭">
          <Icon as={X} size={14} />
        </button>
      </div>
      <div className="panel-b">
        <div className="grp">
          <div className="grp-h">本链配置</div>
          {configs.map((config) => (
            <div className="frow cfg-row" key={config.id}>
              <span className={`cfg-dot${config.enabled ? " live" : ""}`} />
              <b className="cfg-name">{config.label}</b>
              {config.enabled ? <em className="cfg-tag on">生效</em> : null}
              {config.locked ? <em className="cfg-tag">默认</em> : null}
              <span className="cfg-acts">
                <button
                  type="button"
                  className="btn sec cfg-btn"
                  onClick={() => start({ kind: "copy", from: config }, `${config.label} 副本`)}
                >
                  <Icon as={Copy} size={12} />
                  复制
                </button>
                <button
                  type="button"
                  className="btn sec cfg-btn"
                  onClick={() => start({ kind: "rename", target: config }, config.label)}
                >
                  <Icon as={Pencil} size={12} />
                  重命名
                </button>
                {/* A disabled button swallows its own title in every browser that matters,
                    and both reasons here are things the operator has to act on - so the
                    sentence rides on the span around it, where a hover still reaches. */}
                <span
                  className="cfg-tip"
                  title={
                    config.locked
                      ? "默认配置不能删"
                      : config.enabled
                        ? "这是生效配置，先切到别的配置再删"
                        : `删除「${config.label}」和它的报告库`
                  }
                >
                  <button
                    type="button"
                    className="btn sec cfg-btn"
                    disabled={config.locked || config.enabled}
                    onClick={() => start({ kind: "delete", target: config })}
                  >
                    <Icon as={Trash2} size={12} />
                    删除
                  </button>
                </span>
              </span>
            </div>
          ))}
        </div>

        {failure ? (
          <div className="note-bar bad">
            <Icon as={TriangleAlert} size={14} />
            <span>{errorText(failure)}</span>
          </div>
        ) : null}

        {!pending ? (
          <button type="button" className="btn cfg-add" onClick={() => start({ kind: "new" })}>
            <Icon as={Plus} size={12} />
            新建配置
          </button>
        ) : null}

        {creating ? (
          <div className="grp">
            <div className="grp-h">
              {creating.kind === "new" ? "新建配置" : `复制「${creating.from.label}」`}
            </div>
            <div className="frow stack">
              <label>名称</label>
              <input
                className="cfg-input"
                value={label}
                placeholder={
                  creating.kind === "new"
                    ? "留空就是「配置 N」"
                    : `留空就是「${creating.from.label} 副本」`
                }
                onChange={(event) => setLabel(event.target.value)}
              />
              <span className="hint">报告库从空开始。</span>
            </div>
            <div className="frow cfg-foot">
              <button type="button" className="btn sec" onClick={() => done()}>
                取消
              </button>
              <button
                type="button"
                className="btn"
                disabled={add.isPending}
                onClick={() =>
                  add.mutate(
                    {
                      mode: creating.kind,
                      fromConfigId: creating.kind === "copy" ? creating.from.id : undefined,
                      label: label.trim() || undefined,
                    },
                    { onSuccess: (entry) => done(entry.id) },
                  )
                }
              >
                {add.isPending ? "写入中…" : "创建"}
              </button>
            </div>
          </div>
        ) : null}

        {renaming ? (
          <div className="grp">
            <div className="grp-h">重命名「{renaming.target.label}」</div>
            <div className="frow stack">
              <label>名称</label>
              <input
                className="cfg-input"
                value={label}
                onChange={(event) => setLabel(event.target.value)}
              />
              <span className="hint">只改名字，报告库不动。</span>
            </div>
            <div className="frow cfg-foot">
              <button type="button" className="btn sec" onClick={() => done()}>
                取消
              </button>
              <button
                type="button"
                className="btn"
                disabled={rename.isPending || !label.trim()}
                onClick={() =>
                  rename.mutate(
                    { configId: renaming.target.id, label: label.trim() },
                    { onSuccess: () => done() },
                  )
                }
              >
                {rename.isPending ? "写入中…" : "改名"}
              </button>
            </div>
          </div>
        ) : null}

        {deleting ? (
          <div className="grp cfg-danger">
            <div className="grp-h">删除「{deleting.target.label}」</div>
            <div className="frow stack">
              <label>输入配置名</label>
              <input
                className="cfg-input"
                value={label}
                placeholder={deleting.target.label}
                onChange={(event) => setLabel(event.target.value)}
              />
              <span className="hint">
                {detail.data
                  ? `一并删除 ${detail.data.library.runs} 轮 · ${detail.data.library.reports} 卡 · `
                    + `${detail.data.library.entries} 词条 · ${detail.data.library.versions} 版本，不可撤销`
                  : "一并删除这个报告库，不可撤销"}
              </span>
            </div>
            <div className="frow cfg-foot">
              <button type="button" className="btn sec" onClick={() => done()}>
                取消
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={!sameLabel(label, deleting.target.label) || remove.isPending}
                onClick={() =>
                  remove.mutate(
                    { configId: deleting.target.id, label },
                    { onSuccess: () => done() },
                  )
                }
              >
                {remove.isPending ? "删除中…" : "删除"}
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </aside>
  );
}