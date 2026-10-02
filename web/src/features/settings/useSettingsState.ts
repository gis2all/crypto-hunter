import { useEffect, useMemo, useState } from "react";

import type { SettingsField } from "../../api/client";
import type { Run } from "../../api/client";
import { useRunNow, useSaveSettings, useSettingsForm } from "../../api/hooks";
import type { FieldValue } from "./Widget";

/** A patch of dotted-path leaves. The backend coerces each one against the type of
 *  the value it is replacing, so a string from an <input> can never change a type.
 *
 *  A range row is one control over two leaves ("low,high"), so it expands into both
 *  of its keys here instead of teaching the widget about multi-leaf rows. */
function toPatch(
  draft: Record<string, FieldValue>,
  enabled: Record<string, boolean>,
  fields: Map<string, SettingsField>,
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  const put = (key: string, value: unknown) => {
    const parts = key.split(".");
    let node = patch;
    for (const part of parts.slice(0, -1)) {
      node[part] = (node[part] as Record<string, unknown>) ?? {};
      node = node[part] as Record<string, unknown>;
    }
    node[parts[parts.length - 1]] = value;
  };
  for (const [key, value] of Object.entries(draft)) {
    const field = fields.get(key);
    if (field?.key2) {
      const [low, high] = String(value ?? "").split(",");
      put(key, low ?? "");
      put(field.key2, high ?? "");
      continue;
    }
    put(key, value);
  }
  for (const [key, value] of Object.entries(enabled)) {
    put(key, value);
  }
  return patch;
}

/** 设定's state, held above the page because the two buttons that act on it - 保存并生效
 *  and 立即运行 - stand in the topbar beside the tabs, not in the page body. Whoever owns
 *  the buttons owns the draft: a page cannot hand a button to another surface without
 *  handing over the state that makes the button mean anything, so the draft, the two
 *  mutations and the one rule that joins them (save, then run) live here, and both
 *  surfaces read the same object. Both ids travel, because a report library and a round are
 *  scoped by a chain *and* a set of settings - never by the chain alone. */
export function useSettingsState(chainId: string, configId: string, onRunStarted?: (run: Run) => void) {
  const query = useSettingsForm(chainId, configId);
  const save = useSaveSettings(chainId, configId);
  const runNow = useRunNow(chainId, configId, onRunStarted);
  const [draft, setDraft] = useState<Record<string, FieldValue>>({});
  const [enabledDraft, setEnabledDraft] = useState<Record<string, boolean>>({});
  const data = query.data;

  // A draft belongs to the set of settings it was typed against. Carrying it across a move
  // would show edits nobody made on the set now on screen - and the next save would write
  // one library's numbers into another - so a change of scope drops it.
  useEffect(() => {
    setDraft({});
    setEnabledDraft({});
  }, [chainId, configId]);

  const dirtyKeys = useMemo(
    () => new Set(Object.keys(draft).concat(Object.keys(enabledDraft))),
    [draft, enabledDraft],
  );
  const fieldByKey = useMemo(() => {
    const map = new Map<string, SettingsField>();
    for (const block of data?.blocks ?? []) {
      for (const group of block.groups) {
        for (const field of group.fields) {
          map.set(field.key, field);
        }
      }
    }
    return map;
  }, [data]);

  const valueOf = (field: SettingsField): FieldValue =>
    field.key in draft ? draft[field.key] : (field.value as FieldValue);
  const enabledOf = (field: SettingsField): boolean =>
    field.enabled_key ? (enabledDraft[field.enabled_key] ?? Boolean(field.enabled)) : false;
  const isDirty = (field: SettingsField): boolean =>
    dirtyKeys.has(field.key) || Boolean(field.enabled_key && field.enabled_key in enabledDraft);
  const setValue = (field: SettingsField, next: FieldValue) =>
    setDraft((prev) => ({ ...prev, [field.key]: next }));
  const setEnabled = (key: string, next: boolean) =>
    setEnabledDraft((prev) => ({ ...prev, [key]: next }));

  /** Save the draft, and answer whether it landed.
   *
   *  A save the backend refuses is reported by the bar above the form (`save.error`), so
   *  the promise resolves either way: `void commit()` has nowhere to put a second copy of
   *  the reason and must not leave an unhandled rejection in the console. 立即运行 reads
   *  the answer, so it never starts a round under settings that did not save. */
  const commit = (): Promise<boolean> => {
    if (Object.keys(draft).length === 0 && Object.keys(enabledDraft).length === 0) {
      return Promise.resolve(true);
    }
    const patch = toPatch(draft, enabledDraft, fieldByKey);
    return save.mutateAsync(patch).then(
      () => {
        setDraft({});
        setEnabledDraft({});
        return true;
      },
      () => false,
    );
  };

  const groupDirty = (fields: SettingsField[]) => fields.filter(isDirty).length;

  // The bar shipped as runs because it drew the weights and the three averages in bold;
  // the popover has one register, so the sentence is flattened back into one line.
  const noteText = (data?.note_bar ?? []).map((segment) => segment.text).join("");

  return {
    query, save, runNow, data, dirtyKeys, valueOf, enabledOf, isDirty,
    setValue, setEnabled, commit, groupDirty, noteText,
  };
}

export type SettingsState = ReturnType<typeof useSettingsState>;
