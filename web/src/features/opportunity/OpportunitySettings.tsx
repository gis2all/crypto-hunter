/** 新机会的设定页：字段、范围、分组与说明全部来自后端。
 *
 * 草稿与保存状态住在 `useOpportunitySettingsState`，因为 保存并生效 / 立即运行 在顶栏，
 * 不在页面底部。这里只负责画：脏条、错误、三块 ARC 风格的分组卡。
 */
import { Activity, Boxes, Clock, Inbox, SlidersHorizontal } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { errorText } from "../../api/client";
import { Icon } from "../../components/Icon";
import { NoteHelp } from "../settings/NoteHelp";
import type { OpportunitySettingsState } from "./useOpportunitySettingsState";

const BLOCK_ICON: Record<string, LucideIcon> = {
  collect: Inbox,
  analyze: Boxes,
  query: Activity,
  run: Clock,
};

export function OpportunitySettings({ state }: { state: OpportunitySettingsState }) {
  const { query, fields, blocks, draft, dirty, dirtyCount, setValue, error } = state;

  if (query.isError) {
    return <div className="note-bar bad"><span>{errorText(query.error)}</span></div>;
  }

  // A response without blocks still draws every knob, so a stale read never hides a setting.
  const layout = blocks.length ? blocks : [{
    key: "all", title: "参数", columns: 1,
    groups: [{ key: "all", title: "参数", fields }],
  }];

  return (
    <section className="params opp-settings">
      <div className="dirtybar" hidden={!dirty}>
        <b>参数已改 <i>{dirtyCount}</i> 项</b>
        {` \u2014\u2014 ${query.data?.dirty_hint ?? "保存并生效后，下一轮扫描使用新值。"}`}
      </div>
      {error ? <div className="note-bar bad"><span>{error}</span></div> : null}
      {layout.map((block) => (
        <div className="blk" key={block.key}>
          <div className="blk-t">
            <Icon as={BLOCK_ICON[block.key] ?? SlidersHorizontal} size={14} />
            <b>{block.title}</b>
          </div>
          <div className={block.columns > 1 ? `blk-cols cols${block.columns}` : "blk-cols"}>
            {block.groups.map((group) => (
              <div className="pgroup" key={group.key}>
                <div className="pgroup-h">
                  <b>{group.title}</b>
                  <NoteHelp text={group.fields.map((field) =>
                    `${field.label}：${field.hint}`).join("\n")} />
                </div>
                <div className="prows">
                  {group.fields.map((field) => (
                    <div className="prow" key={field.key}>
                      <label>{field.label}</label>
                      <span className="field">
                        {field.kind === "text" ? (
                          <span className="derived">{String(field.value ?? "—")}</span>
                        ) : (
                          <>
                            <input
                              className="num"
                              type="number"
                              value={draft[field.key] ?? ""}
                              min={field.min ?? undefined}
                              max={field.max ?? undefined}
                              step={field.kind === "float" ? "0.5" : "1"}
                              onChange={(event) => setValue(field.key, event.target.value)}
                            />
                            <span className="unit">
                              {(field.min ?? 0) < 0
                                ? "−1 全部 · 0 关"
                                : `${field.min}–${field.max}`}
                            </span>
                          </>
                        )}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}
