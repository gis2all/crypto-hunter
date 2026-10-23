import { Bot, Save, ScrollText, Search, Settings, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { errorText, getSystemModels, probeXSession, testSystem } from "../../api/client";
import type { SettingsField, SystemModels, SystemTest, SystemX } from "../../api/client";
import { useSaveSystem, useSystemForm } from "../../api/hooks";
import type { XSessionDraft } from "../../api/client";
import { Icon } from "../../components/Icon";
import { Pick } from "../../components/Pick";
import { NoteHelp } from "../settings/NoteHelp";
import { Widget, stringValue, type FieldValue } from "../settings/Widget";

/** One glyph per block, so the three readings are told apart before the words are read. */
const BLOCK_ICON: Record<string, LucideIcon> = {
  llm: Bot,
  x: Search,
  log: ScrollText,
};

/** 系统设置: this machine's own wiring - which gateway answers the writing stage, which X session
 *  carries a search, how loud the log is.
 *
 *  A place rather than a tab (Rail): none of it belongs to a chain or to a set of settings, so
 *  there is nothing for a 报告 / 设定 pair under it to mean. The rows come from the backend's own
 *  table (`config.SYSTEM_KEYS`) and this page reads no key by name - it draws blocks, and the
 *  same widgets draw a chain's 设定.
 *
 *  Two things this page says that a chain's page does not have to: where a value came from (a
 *  variable the shell exported beats the file, so that row is a reading and not a box), and that
 *  a credential is stored without the credential itself ever reaching the browser.
 */
export function SystemView() {
  const query = useSystemForm();
  const save = useSaveSystem();
  const [draft, setDraft] = useState<Record<string, FieldValue>>({});
  const data = query.data;
  /** X 会话列表：和上面的行一起进同一个 draft，走右上角的「保存并生效」。 */
  const [sessions, setSessions] = useState<SessionDraft[]>([]);
  useEffect(() => {
    if (!data) return;
    setSessions((data.sessions ?? []).map((row) => ({
      label: row.label, was_label: row.label, auth_token: "", ct0: "", enabled: row.enabled,
      mask_auth: row.mask_auth, mask_ct0: row.mask_ct0, state: row.state ?? "",
    })));
  }, [data]);

  // What an endpoint answered, held apart from the form on purpose: none of it is saved, and
  // leaving the page is meant to forget it. `choices` is keyed by the row the backend named
  // in its answer, so this page still learns no key of its own.
  const [choices, setChoices] = useState<Record<string, string[]>>({});
  /** The answer beside the buttons: one sentence for 获取模型, one line per key for 测试连接,
   *  one sentence for the X session's probe. */
  const [probe, setProbe] = useState<{ group: string; ok: boolean; note?: string;
    lines?: SystemTest["lines"] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const saved = data?.sessions ?? [];
  const sessionDirty = sessions.length !== saved.length
    || sessions.some((row, at) => row.label !== saved[at]?.label
      || row.auth_token !== "" || row.ct0 !== "");
  const dirty = Object.keys(draft).length + (sessionDirty ? 1 : 0);
  const valueOf = (field: SettingsField): FieldValue =>
    field.key in draft ? draft[field.key] : (field.value as FieldValue);

  // A row's key is its own path in the file, so the patch is the draft as it stands: there is no
  // second shape to build here and nothing a compound row would have to expand into.
  const commit = () => {
    if (dirty === 0) {
      return;
    }
    save.mutate({ patch: draft, sessions }, { onSuccess: () => setDraft({}) });
  };

  // A probe reads the rows as they stand on screen - the draft, not the file - so a wrong
  // endpoint can be found out before it is written down. A refusal is not an exception here:
  // it is the sentence the operator is after, printed beside the button that asked.
  const run = (action: string, groupKey: string) => {
    if (action === "add-session") {
      setSessions((prev) => [...prev, {
        label: "cookie" + (prev.length + 1), was_label: "", auth_token: "", ct0: "", enabled: true,
        mask_auth: "", mask_ct0: "", state: ""}]);
      return;
    }
    setBusy(action);
    // One union, named: three answers of three shapes go through one handler below.
    const call: Promise<SystemModels | SystemTest | SystemX> = action === "models"
      ? getSystemModels(draft)
      : action === "x"
        ? probeXSession(draft, sessions.map(({ label, was_label, auth_token, ct0, enabled }) =>
            ({ label, was_label, auth_token, ct0, enabled })))
        : testSystem(draft);
    call
      .then((answer) => {
        // A list of lines is the shape two of the three answers can take; what is left is a
        // sentence, and 获取模型 also hands back the models it found.
        const lines = "lines" in answer ? answer.lines : undefined;
        if (lines?.length) {
          setProbe({ group: groupKey, ok: answer.ok, lines });
          return;
        }
        if ("apply_to" in answer && answer.apply_to && answer.models.length) {
          setChoices((was) => ({ ...was, [answer.apply_to]: answer.models }));
        }
        setProbe({ group: groupKey, ok: answer.ok, note: "note" in answer ? answer.note : "" });
      })
      .catch((err) => setProbe({ group: groupKey, ok: false, note: errorText(err) }))
      .finally(() => setBusy(null));
  };

  const head = (
    <div className="fav-bar">
      <div className="fav-title">
        <Icon as={Settings} size={14} />
        <b>系统设置</b>
      </div>
      <div className="fav-tools">
        <button
          className="btn"
          id="sys-save-btn"
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
      <div id="view-system">
        {head}
        <section className="params">
          <div className={query.isError ? "note-bar bad" : "note-bar"}>
            {query.isError ? <Icon as={TriangleAlert} size={14} /> : null}
            {query.isError ? "系统设置读取失败：" + errorText(query.error) : "读取中…"}
          </div>
        </section>
      </div>
    );
  }

  return (
    <div id="view-system">
      {head}
      <section className="params">
        <div className="dirtybar" hidden={dirty === 0}>
          <b>
            已改 <i>{dirty}</i> 项
          </b>
          —— 保存后生效
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
                    <NoteHelp text={group.detail} />
                  </div>
                  <div className="prows">
                    {group.key === "x-session" ? sessions.map((row, index) => (
                      <div className="sess-block" key={index}>
                        <div className="prow sess-head">
                          <label>
                            会话 {index + 1}
                            {row.state === "disabled" ? <span className="dim">已停用</span>
                              : row.state === "cooling" ? <span className="dim">冷却中</span>
                                : null}
                          </label>
                          <span className="spacer" />
                          <button type="button" className="btn sec"
                                  onClick={() => setSessions((prev) =>
                                    prev.filter((_, at) => at !== index))}>
                            移除
                          </button>
                        </div>
                        <div className="prow sess">
                          <label>标签</label>
                          <span className="field">
                            <input className="num" value={row.label}
                                   onChange={(event) => setSessions((prev) => prev.map((item, at) =>
                                     at === index ? { ...item, label: event.target.value } : item))} />
                          </span>
                        </div>
                        <div className="prow sess">
                          <label>auth_token</label>
                          <span className="field">
                            <input className="num" type="password" value={row.auth_token}
                                   onChange={(event) => setSessions((prev) => prev.map((item, at) =>
                                     at === index
                                       ? { ...item, auth_token: event.target.value } : item))} />
                            <span className="dim">
                              {row.auth_token ? "未保存"
                                : row.mask_auth ? "已配置 · " + row.mask_auth : "未配置"}
                            </span>
                          </span>
                        </div>
                        <div className="prow sess">
                          <label>ct0</label>
                          <span className="field">
                            <input className="num" type="password" value={row.ct0}
                                   onChange={(event) => setSessions((prev) => prev.map((item, at) =>
                                     at === index ? { ...item, ct0: event.target.value } : item))} />
                            <span className="dim">
                              {row.ct0 ? "未保存"
                                : row.mask_ct0 ? "已配置 · " + row.mask_ct0 : "未配置"}
                            </span>
                          </span>
                        </div>
                      </div>
                    )) : null}
                    {group.fields.map((field) => {
                      // A row the operating system owns arrives as a reading ("ro"), the same
                      // shape the settings page uses for a value this page cannot edit.
                      const ro = field.widget === "ro";
                      const rowClass = ["prow", ro ? "ro" : "", field.key in draft ? "dirty" : ""]
                        .filter(Boolean)
                        .join(" ");
                      // A picker, where the row has an answer to offer: the backend's usual
                      // gateways, or the list this endpoint just gave. It only *types* one
                      // into the box beside it, which stays a box - a model the gateway does
                      // not list is still one it answers with.
                      const offered = field.presets?.length
                        ? field.presets
                        : (choices[field.key] ?? []).map((name) => ({ value: name, label: name }));
                      const setValue = (next: FieldValue) =>
                        setDraft((prev) => ({ ...prev, [field.key]: next }));
                      return (
                        <div className={rowClass} key={field.key}>
                          <label>
                            {field.label}
                            {field.dim && field.dim_in === "label" ? (
                              <span className="dim">{field.dim}</span>
                            ) : null}
                          </label>
                          <span className={ro ? "field mono" : "field"}>
                            <Widget
                              field={field}
                              value={valueOf(field)}
                              enabled={undefined}
                              onValue={setValue}
                              onEnabled={() => undefined}
                            />
                            {field.dim && field.dim_in === "field" ? (
                              <span className="dim">{field.dim}</span>
                            ) : null}
                            {offered.length ? (
                              <Pick
                                className="btn sec"
                                label={field.presets?.length ? "常用网关" : "端点给的模型"}
                                current={stringValue(valueOf(field))}
                                trigger={
                                  <span className="pick-cur">
                                    {field.presets?.length ? "预设" : "候选"}
                                  </span>
                                }
                                options={offered}
                                onPick={setValue}
                              />
                            ) : null}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                  {group.actions.length ? (
                    <div className="pgroup-acts">
                      {group.actions.map((action) => (
                        <button
                          type="button"
                          key={action.id}
                          className="btn sec"
                          disabled={busy !== null}
                          onClick={() => run(action.id, group.key)}
                        >
                          {action.label}
                        </button>
                      ))}
                      {probe && probe.group === group.key ? (
                        probe.lines ? (
                          <span className="pact-lines">
                            {probe.lines.map((line, index) => (
                              <span className={line.ok ? "pact-line" : "pact-line bad"}
                                    key={index}>
                                {line.label ? <b>{line.label} ·</b> : null}
                                <span>{line.note}</span>
                              </span>
                            ))}
                          </span>
                        ) : (
                          <span className={probe.ok ? "pact-note" : "pact-note bad"}>
                            {probe.note}
                          </span>
                        )
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}

type SessionDraft = XSessionDraft & { mask_auth: string; mask_ct0: string };
