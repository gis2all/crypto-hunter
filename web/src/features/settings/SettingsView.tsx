import { Activity, Boxes, ShieldCheck, SlidersHorizontal, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Fragment } from "react";

import { errorText } from "../../api/client";
import type { SettingsBlock, SettingsField, SettingsForm } from "../../api/client";
import { Icon } from "../../components/Icon";
import { NoteHelp } from "./NoteHelp";
import { type SettingsState } from "./useSettingsState";
import { Widget } from "./Widget";

/** Read-only rows print their value in the mono face: the frozen prototype marks every
 *  one of them `field mono`, whether the value comes from the config or from the run. */
/** Groups read as two columns, and where the left column ends.
 *
 * 左栏读完再读右栏, so the cut is a row index and it has to land on a semantic
 * boundary: 硬过滤 cuts after the five threshold rows (`捆绑率`).
 *
 * Everywhere else the rows stay in one column - not because they are short, but
 * because nothing in them has to wrap. `判定候选数` (运行) is 45px at 470 wide, 104px
 * at 400, 266px at 370: splitting a group like that only makes it taller. */
const SPLIT_AT: Record<string, number> = { hard: 5 };

/** One glyph per block, so the three readings this page is built on are told apart before
 *  the words are read. The first two are the marks the cards already draw for the same two
 *  names (the 打分行 uses ShieldCheck / Boxes), so a reader who met one on a card meets the
 *  same mark here. 运行 has no card twin: Activity is the run itself. */
const BLOCK_ICON: Record<string, LucideIcon> = {
  onchain: ShieldCheck,
  offchain: Boxes,
  run: Activity,
};

function readOnly(field: SettingsField): boolean {
  return Boolean(field.ro_row || field.ro_value) || field.widget === "ro";
}

export function SettingsView({ state }: { state: SettingsState }) {
  const {
    query, save, runNow, data, dirtyKeys, valueOf, enabledOf, isDirty,
    setValue, setEnabled, groupDirty, noteText,
  } = state;

  if (!data) {
    return (
      <section className="params" id="view-params">
        <div className="params-head">
          <h2>
          <Icon as={SlidersHorizontal} size={16} />
          参数设置
        </h2>
        </div>
        <div className={query.isError ? "note-bar bad" : "note-bar"}>
          {query.isError ? <Icon as={TriangleAlert} size={14} /> : null}
          {query.isError ? `设定读取失败：${errorText(query.error)}` : "读取中…"}
        </div>
      </section>
    );
  }

  const renderGroup = (group: SettingsForm["blocks"][number]["groups"][number]) => {
    const rows = group.fields;
    const wide = group.fields.some((field) => field.widget === "checks");

    const renderRow = (field: SettingsField, index: number) => {
      if (field.widget === "sub") {
        return (
          <div className="psub" key={`${field.key}-${index}`}>
            {field.label}
          </div>
        );
      }
      const dirty = isDirty(field);
      const rowClass = [
        "prow",
        wide && field.widget === "checks" ? "wide" : "",
        field.ro_row ? "ro" : "",
        dirty ? "dirty" : "",
      ]
        .filter(Boolean)
        .join(" ");
      return (
        <div className={rowClass} key={`${field.key}-${index}`}>
          <label>
            {field.label}
            {field.dim && field.dim_in === "label" ? (
              <span className="dim">{field.dim}</span>
            ) : null}
          </label>
          {field.drop_count === null || field.drop_count === undefined ? (
            field.drop_x ? (
              <em className="drop">
                <span className="drop-x">{field.drop_x}</span>
              </em>
            ) : null
          ) : (
            <em className="drop">
              <span className="drop-n">
                {field.drop_count === 0
                  ? "—"
                  : `${field.drop_prefix}${field.drop_count.toLocaleString()}`}
              </span>
              <span className="drop-x">{field.drop_x || "待重算"}</span>
            </em>
          )}
          <span className={readOnly(field) ? "field mono" : "field"}>
            <Widget
              field={field}
              value={valueOf(field)}
              enabled={field.enabled_key ? enabledOf(field) : undefined}
              onValue={(next) => setValue(field, next)}
              onEnabled={(next) => field.enabled_key && setEnabled(field.enabled_key, next)}
            />
            {field.warn ? <span className="viol">{field.warn}</span> : null}
          </span>
        </div>
      );
    };

    const cut = SPLIT_AT[group.key];
    return (
      <div className={`pgroup${cut ? " split" : ""}`} key={group.key} id={`card-${group.key}`}>
        <div className="pgroup-h">
          <b>{group.title}</b>
          {group.hint ? <em>{group.hint}</em> : null}
          {/* The unsaved count used to sit in the header as a chip. It is the only line here
              the backend cannot know (it describes the draft), so it travels as the popover's
              first line instead - one 说明 control, one place to read. */}
          <NoteHelp
            text={group.detail}
            lead={groupDirty(group.fields) === 0 ? "" : `待改 ${groupDirty(group.fields)} 项`}
          />
        </div>
        <div className="prows">
          {cut ? (
            <>
              <div className="pcol">{rows.slice(0, cut).map(renderRow)}</div>
              <div className="pcol">{rows.slice(cut).map(renderRow)}</div>
            </>
          ) : (
            rows.map(renderRow)
          )}
        </div>

        {group.note ? (
          <div className="pset">
            {group.note.split("\n").map((line, index) => (
              <Fragment key={index}>
                {index > 0 ? <br /> : null}
                {line}
              </Fragment>
            ))}
          </div>
        ) : null}
      </div>
    );
  };

  const renderBlock = (block: SettingsBlock) => (
    <div className="blk" key={block.key}>
      <div className="blk-t">
        <Icon as={BLOCK_ICON[block.key]} size={14} />
        <b>{block.title}</b>
      </div>
      <div className={`blk-cols cols${block.columns}`} data-blk={block.key}>
        {block.groups.map(renderGroup)}
      </div>
    </div>
  );

  return (
    <section className="params" id="view-params">
      <div className="params-head">
        <h2>
          <Icon as={SlidersHorizontal} size={16} />
          参数设置
        </h2>
        <span className="chain">
          {data.label}
        </span>
        {/* The composite and this round's measured averages used to sit in a bar under this
            header, and the save rule behind an icon in that bar. Both are read once. The two
            buttons that act on them (保存并生效 / 立即运行) stand in the topbar now, in the
            place the round's own actions take on the report page - so the header keeps only
            what is read, and the row of controls exists in one place at a time. */}
        <NoteHelp text={[noteText, data.note_detail].filter(Boolean).join("\n")} />
      </div>
      {/* The bar lives in the DOM either way and is hidden while nothing is pending, so
          the page keeps the frozen prototype's markup and never reflows on the first edit. */}
      <div className="dirtybar" hidden={dirtyKeys.size === 0}>
        <b>
          参数已改 <i>{dirtyKeys.size}</i> 项
        </b>
        {` —— ${data.dirty_hint}`}
      </div>
      {save.isError ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>保存失败：{errorText(save.error)}</span>
        </div>
      ) : null}
      {runNow.isError ? (
        <div className="note-bar bad">
          <Icon as={TriangleAlert} size={14} />
          <span>运行失败：{errorText(runNow.error)}</span>
        </div>
      ) : null}
      {data.blocks.map(renderBlock)}
    </section>
  );
}
