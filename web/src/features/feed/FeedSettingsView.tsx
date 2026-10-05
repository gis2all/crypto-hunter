import { CalendarClock, Gauge, Save, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";

import { errorText } from "../../api/client";
import type { SettingsField } from "../../api/client";
import { useFeedSettingsForm, useSaveFeedSettings } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { NoteHelp } from "../settings/NoteHelp";
import { Widget, type FieldValue } from "../settings/Widget";

/** One glyph per block, so 出刊 and 闸门 are told apart before the words are read. */
const BLOCK_ICON: Record<string, LucideIcon> = {
  schedule: CalendarClock,
  budget: Gauge,
};

/** 信息流设定: 什么时候出刊, 以及一轮往补正文和写作各花多少.

 *  和 系统设置 同一套门面 (8.9): 「config/feed/pipeline.yaml」是唯一真相, 这一页只是它的表 ——
 *  所以这里不认任何键名, 它画 blocks, 而画它们的是链条设定同一批 widget. 一次保存写回文件,
 *  下一轮 tick 就读到新值: 不用重启, 也不在页面里留第二份真相.

 *  它是左栏 信息流 那五行下面的第六行, 而不是一个 tab (用户定的, 2026-10-05): 出刊节奏和两道
 *  闸门不属于 精选 / 全部动态 / 热点榜 / 主题 / 报刊 里任何一个, 所以它不能是它们中间的一页 ——
 *  它是这一栏的另一件事.
 */
export function FeedSettingsView() {
  const query = useFeedSettingsForm();
  const save = useSaveFeedSettings();
  const [draft, setDraft] = useState<Record<string, FieldValue>>({});
  const data = query.data;

  const dirty = Object.keys(draft).length;
  const valueOf = (field: SettingsField): FieldValue =>
    field.key in draft ? draft[field.key] : (field.value as FieldValue);

  // A row is keyed by its own path in the file, so the patch is the draft as it stands:
  // there is no second shape to build here.
  const commit = () => {
    if (dirty === 0) {
      return;
    }
    save.mutate(draft, { onSuccess: () => setDraft({}) });
  };

  const head = (
    <div className="fav-bar">
      <div className="fav-title">
        <Icon as={CalendarClock} size={14} />
        <b>信息流设定</b>
      </div>
      <div className="fav-tools">
        <button
          className="btn"
          id="feed-save-btn"
          disabled={dirty === 0 || save.isPending}
          onClick={commit}
        >
          <Icon as={Save} size={14} />
          保存并生效
        </button>
      </div>
    </div>
  );

  if (!data) {
    return (
      <div id="view-feed-settings">
        {head}
        <section className="params">
          <div className={query.isError ? "note-bar bad" : "note-bar"}>
            {query.isError ? <Icon as={TriangleAlert} size={14} /> : null}
            {query.isError ? "信息流设定读取失败：" + errorText(query.error) : "读取中…"}
          </div>
        </section>
      </div>
    );
  }

  return (
    <div id="view-feed-settings">
      {head}
      <section className="params">
        <div className="dirtybar" hidden={dirty === 0}>
          <b>
            已改 <i>{dirty}</i> 项
          </b>
          —— 下一轮起生效
        </div>
        {save.isError ? (
          <div className="note-bar bad">
            <Icon as={TriangleAlert} size={14} />
            <span>保存失败：{errorText(save.error)}</span>
          </div>
        ) : null}
        {data.blocks.map((block) => (
          <div className="blk" key={block.key}>
            <div className="blk-t">
              <Icon as={BLOCK_ICON[block.key]} size={14} />
              <b>{block.title}</b>
            </div>
            <div className={"blk-cols cols" + block.columns} data-blk={block.key}>
              {block.groups.map((group) => (
                <div className="pgroup" key={group.key} id={"card-" + group.key}>
                  <div className="pgroup-h">
                    <b>{group.title}</b>
                    {group.hint ? <em>{group.hint}</em> : null}
                    <NoteHelp text={group.detail} />
                  </div>
                  <div className="prows">
                    {group.fields.map((field) => (
                      <div
                        className={["prow", field.key in draft ? "dirty" : ""]
                          .filter(Boolean)
                          .join(" ")}
                        key={field.key}
                      >
                        <label>{field.label}</label>
                        <span className="field">
                          <Widget
                            field={field}
                            value={valueOf(field)}
                            enabled={undefined}
                            onValue={(next) =>
                              setDraft((prev) => ({ ...prev, [field.key]: next }))
                            }
                            onEnabled={() => undefined}
                          />
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
    </div>
  );
}
