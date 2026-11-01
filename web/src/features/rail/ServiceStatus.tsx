import type { Health } from "../../api/client";
import { useHealth } from "../../api/hooks";

/** The rail's health line: only the readings that are not "ok" get a row.
 *
 *  Nothing here probes a service. The backend reads the calls this app already made and the
 *  session pool's own state, so a page left open all day costs one local query a minute
 *  (2026-10-31). */
export function ServiceStatus({ onOpen }: { onOpen: (anchor: string) => void }) {
  const health = useHealth();
  const candidates: { anchor: string; state: Health["llm_state"] | undefined }[] = [
    { anchor: "card-llm-endpoint", state: health.data?.llm_state },
    { anchor: "card-x-session", state: health.data?.x_state },
  ];
  const rows = candidates.filter(
    (row): row is { anchor: string; state: Health["llm_state"] } =>
      Boolean(row.state && row.state.level !== "ok"),
  );
  if (!rows.length) {
    return null;
  }
  return (
    <div className="svc-status">
      {rows.map((row) => (
        <button
          key={row.anchor}
          type="button"
          className={"svc-row " + row.state.level}
          title={row.state.detail || row.state.label}
          onClick={() => onOpen(row.anchor)}
        >
          <i aria-hidden="true" />
          <span>{row.state.label}</span>
        </button>
      ))}
    </div>
  );
}
