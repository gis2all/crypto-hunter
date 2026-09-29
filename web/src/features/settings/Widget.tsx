import type { SettingsField } from "../../api/client";

import { Pick } from "../../components/Pick";

export type FieldValue = string | number | boolean | string[] | null;

export function stringValue(value: FieldValue): string {
  if (value === null || value === undefined) {
    return "";
  }
  return Array.isArray(value) ? value.join(",") : String(value);
}

export function listValue(value: FieldValue): string[] {
  return Array.isArray(value) ? value : [];
}

/** One settings row. The widget kind comes from the backend, so this file never
 *  learns what a filter or a weight is. */
export function Widget({
  field,
  value,
  enabled,
  onValue,
  onEnabled,
}: {
  field: SettingsField;
  value: FieldValue;
  enabled: boolean | undefined;
  onValue: (next: FieldValue) => void;
  onEnabled: (next: boolean) => void;
}) {
  const unit = field.unit ?? "";
  const prefix = unit === "$" ? <span className="unit">{unit}</span> : null;
  const suffix = unit && unit !== "$" ? <span className="unit">{unit}</span> : null;

  if (field.widget === "range") {
    const [low, high] = stringValue(value).split(",");
    // The two ends of a band. A currency mark is a prefix and rides on both ends, the way
    // the prototype drew 市值 ($0 - $200K); any other unit is a counter and reads once,
    // after the band (100 - 1000 个). '个 100 - 个 1000' is not a sentence anyone writes.
    const prefixUnit = unit === "$" ? <span className="unit">{unit}</span> : null;
    return (
      <>
        {prefixUnit}
        <input className="num" value={low ?? ""} onChange={(e) => onValue(`${e.target.value},${high ?? ""}`)} />
        <span className="op">–</span>
        {prefixUnit}
        <input className="num" value={high ?? ""} onChange={(e) => onValue(`${low ?? ""},${e.target.value}`)} />
        {unit && unit !== "$" ? <span className="unit">{unit}</span> : null}
      </>
    );
  }

  if (field.widget === "num") {
    return (
      <>
        {field.op ? <span className="op">{field.op}</span> : null}
        {prefix}
        <input className="num" value={stringValue(value)} onChange={(e) => onValue(e.target.value)} />
        {suffix}
        {field.dim && field.dim_in === "field" ? <span className="dim">{field.dim}</span> : null}
        {field.enabled_key ? (
          <label className="chk">
            <input type="checkbox" checked={Boolean(enabled)} onChange={(e) => onEnabled(e.target.checked)} />
            开启
          </label>
        ) : null}
      </>
    );
  }

  if (field.widget === "bool") {
    return (
      <label className="chk">
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onValue(e.target.checked)} />
        {field.check_label || "开启"}
      </label>
    );
  }

  if (field.widget === "select") {
    const current = stringValue(value);
    const shown = field.options.find((option) => option.value === current)?.label ?? current;
    return (
      <Pick
        className="btn sec"
        label={field.label}
        current={current}
        trigger={<span className="pick-cur">{shown}</span>}
        options={field.options.map((option) => ({ value: option.value, label: option.label }))}
        onPick={onValue}
      />
    );
  }

  if (field.widget === "checks") {
    const chosen = new Set(listValue(value));
    return (
      <>
        {field.checks.map((check) => (
          <label className="chk" key={check.value}>
            <input
              type="checkbox"
              value={check.value}
              checked={chosen.has(check.value)}
              onChange={(e) => {
                const next = new Set(chosen);
                if (e.target.checked) {
                  next.add(check.value);
                } else {
                  next.delete(check.value);
                }
                onValue(field.checks.filter((c) => next.has(c.value)).map((c) => c.value));
              }}
            />
            {check.label}
            {check.count === null || check.count === undefined ? null : (
              <span className="cd">{check.count}</span>
            )}
          </label>
        ))}
      </>
    );
  }

  if (field.widget === "derived") {
    return (
      <>
        <span className="derived mono">{stringValue(value)}</span>
        {field.dim && field.dim_in === "field" ? <span className="dim">{field.dim}</span> : null}
      </>
    );
  }

  if (field.ro_value) {
    return (
      <span className="ro-v" title={stringValue(value)}>
        {stringValue(value)}
      </span>
    );
  }
  return <>{stringValue(value)}</>;
}
