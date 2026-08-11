import { useEffect, useMemo, useState } from "react";

import { errorText, type OpportunitySetting } from "../../api/client";
import { useOpportunitySettings, useSaveOpportunitySettings } from "../../api/hooks";

/** The settings draft, held above the page because 保存并生效 / 立即运行 stand in the topbar.
 *
 * The topbar and the settings page must read one draft; a page-local state would make the
 * topbar buttons act on a copy nobody can see. The save rule stays here too: 立即运行 must
 * commit first and must not start a scan under values that failed to save.
 */
export function useOpportunitySettingsState() {
  const query = useOpportunitySettings();
  const save = useSaveOpportunitySettings();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  const fields: OpportunitySetting[] = query.data?.fields ?? [];
  const blocks = query.data?.blocks ?? [];

  // The answer is the truth: every read resets the draft, so a saved value and the box agree.
  useEffect(() => {
    if (fields.length) {
      setDraft(Object.fromEntries(fields.map((field) => [field.key, String(field.value)])));
    }
  }, [query.data]);

  // The effect above fills the draft after the query lands. Until every key is present,
  // an empty draft would read as "all fields changed" and flash a dirty bar on arrival.
  const ready = fields.length > 0 && fields.every((field) => field.key in draft);
  const dirtyFields = useMemo(
    () => ready ? fields.filter((field) => draft[field.key] !== String(field.value)) : [],
    [fields, draft, ready],
  );

  const setValue = (key: string, value: string) =>
    setDraft((prev) => ({ ...prev, [key]: value }));

  const commit = async (): Promise<boolean> => {
    setError("");
    if (!ready) {
      if (query.isError) {
        setError(errorText(query.error));
      }
      return false;
    }
    // 立即运行 with a clean form must not create a settings file just to run.
    if (!dirtyFields.length) return true;
    const values: Record<string, number> = {};
    for (const field of dirtyFields) {
      const raw = draft[field.key] ?? "";
      if (raw.trim() === "") {
        setError(`${field.label} 需要一个数字`);
        return false;
      }
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        setError(`${field.label} 需要一个数字`);
        return false;
      }
      values[field.key] = value;
    }
    try {
      await save.mutateAsync(values);
      return true;
    } catch (exc) {
      setError(errorText(exc));
      return false;
    }
  };

  return {
    query, save, fields, blocks, draft, ready,
    dirty: dirtyFields.length > 0, dirtyCount: dirtyFields.length,
    setValue, commit, error,
  };
}

export type OpportunitySettingsState = ReturnType<typeof useOpportunitySettingsState>;
