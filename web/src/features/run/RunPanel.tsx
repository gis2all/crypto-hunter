import { Circle, CircleCheck, CircleX, LoaderCircle, Pause } from "lucide-react";
import { useEffect, useState } from "react";

import type { Batch, Run } from "../../api/client";
import { Icon } from "../../components/Icon";

/** The live run panel.
 *
 * It shows the round's real state read from the database and never a fake progress
 * bar: the percent comes from `runs.progress`, which the pipeline writes at its own
 * checkpoints (sources pulled, candidates judged, draft batches written), and the phase
 * name next to it is the phase that percent belongs to.
 *
 * The stage markers follow the frozen stylesheet: a step is `done / run / pending` on the
 * <li>. Every state keeps its step list on screen - finished, failed and cancelled alike -
 * because the right column is where the round own timings are read, and folding it away the
 * moment the last stage ended is how the answer to "what took the time" got written and then
 * hidden. A round that is queued but not started yet keeps the bare `rst` markers and the
 * `本轮 · 待运行` header of the prototype pre-run state. The panel carries no 收起 of its
 * own: folding is the 运行结果 switch in the topbar (8.3.7), which is the only control that
 * ever needs to know where the panel is.
 *
 * A round the operator stopped (`cancelled`) says 已中止 in the header - the answer to the
 * button that was just pressed - and lists the phases that had already ended, so a round
 * stopped during the model pass still shows how far it got.
 *
 * The prototype also prints an estimated duration there ("预计 4:12"). That number was a
 * hand-written demo constant, not a measurement, so there is no forecast here: each row's
 * right column shows that phase's own measured wall clock, written by the pipeline's
 * stopwatch the moment the phase ends (`pipeline/progress.py`, `write.store_span`). A row
 * that owns no phase — 推送, which is not wired up — stays blank rather than borrowing a
 * neighbour's seconds.
 */

const STAGES: { key: string; label: string }[] = [
  { key: "collect", label: "拉取扫描源" },
  { key: "dedupe", label: "去重" },
  { key: "prefilter", label: "硬过滤" },
  { key: "score", label: "打分排序" },
  { key: "report", label: "生成报告" },
  { key: "push", label: "推送 Telegram" },
];

const ACTIVE_STAGE: Record<string, number> = {
  pending: 0,
  collected: 2,
  scored: 3,
  selected: 4,
  written: 5,
  researched: 6,
};

/** Which stopwatch phases each row is made of.
 *
 * 拉取 and 去重 are one network stretch, split where the last source has answered, so each
 * owns its own seconds. 生成报告 is the model writing: the reading pass that produces each
 * entry, and the write that stores the round. 硬过滤 carries every read it needs to judge a
 * row - the chip top-up (`topup`) and the per-token gate (`gate`) - because both are part of
 * that filter, not stages of their own. There is one 淘汰 row for the same reason: the pool
 * rules, the safety rules, the trap ceiling and 距高回撤 all reject a token before the round
 * spends a token on it, so splitting them in two was a split of nothing.
 */
const STAGE_PHASES: Record<string, string[]> = {
  collect: ["pull"],
  dedupe: ["dedupe"],
  prefilter: ["prefilter", "topup", "gate"],
  score: ["score", "sharpen"],
  report: ["draft", "write"],
  push: [],
};

/** Every scanner the two chains run, so a Robinhood round names its own sources
 *  (CoinBarrel / PEZ / Peeps) rather than printing the raw key. */
const SOURCE_SHORT: Record<string, string> = {
  gmgn: "GMGN",
  radardex: "RadarDEX",
  tolly: "Tolly",
  coinbarrel: "CoinBarrel",
  pez: "PEZ",
  peeps: "Peeps",
  geckoterminal: "GeckoTerminal",
};

/** A stage's own wall clock, as the panel prints it. Shared with the CA search panel
 *  (features/lookup), so the two step lists spell a duration the same way. */
export function mmss(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function stamp(value: string | null | undefined): number | null {
  const ms = value ? new Date(value).getTime() : NaN;
  return Number.isNaN(ms) ? null : ms;
}

function elapsed(run: Run, running: boolean, now: number): string {
  const start = stamp(run.started_at);
  if (start === null) {
    return "0:00";
  }
  const end = running ? now : (stamp(run.finished_at) ?? now);
  return mmss(Math.max(0, Math.round((end - start) / 1000)));
}

/** The marker in front of a stage.
 *
 *  Four states, not three. 未到 / 正在 / 完成 are the round's own progress; 停在这里 is the
 *  one a failed round needs and could never say with a filled square - the round it died on
 *  keeps the panel open on purpose, and a row of seven identical hollow circles does not
 *  answer the question the panel was left open for.
 *
 *  Only 正在 moves, and it is the only thing on the panel that does. */
/** The marker in front of a stage, shared with the CA search panel for the same reason:
 *  four states, one drawing of each. */
export function Marker({ state }: { state: "pending" | "run" | "done" | "failed" }) {
  if (state === "done") return <Icon as={CircleCheck} />;
  if (state === "run") return <Icon as={LoaderCircle} className="spin" />;
  if (state === "failed") return <Icon as={CircleX} />;
  return <Icon as={Circle} />;
}

export function RunPanel({
  run,
  running,
  open,
  batch,
  onCancel,
  cancelling,
}: {
  run: Run | undefined;
  running: boolean;
  open: boolean;
  batch: Batch | undefined;
  onCancel: () => Promise<unknown>;
  cancelling: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  // 中止 is two presses: the first one arms it, the second one stops the round. A round is
  // minutes of network work, and one click away from the end of it is not where a mis-click
  // belongs. Armed is a question, not a mode - it expires on its own, so a stray press can
  // never leave a button that stops a *later* round on its second click.
  const [armed, setArmed] = useState(false);
  // 已请求中止. The press outlives the request that carries it. The POST is back in
  // milliseconds, while the round works on until it reaches its own next checkpoint -
  // seconds, or one token's network wait - and reading the request's state instead answered
  // "has it stopped" with yes a moment after the click: an 中止 button came back on a
  // round already on its way out, where a second press looked like it did something.
  const [asked, setAsked] = useState(false);
  useEffect(() => {
    setArmed(false);
    setAsked(false);
  }, [run?.id, running]);
  useEffect(() => {
    if (!armed) {
      return;
    }
    // Long enough to read the question and answer it: this press is a decision about a round
    // that is minutes long, and four seconds is not a decision.
    const timer = window.setTimeout(() => setArmed(false), 10000);
    return () => window.clearTimeout(timer);
  }, [armed]);
  useEffect(() => {
    if (!running) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  if (!run) {
    return null;
  }
  const failed = run.status === "failed";
  const cancelled = run.status === "cancelled";
  // 待运行 means "not started", not "the status still says pending". The pipeline stamps
  // `started_at` when the worker picks the round up and leaves the status at `pending`
  // until the pull is over - a minute and a half of sources - so a round that is already
  // fetching would otherwise still read 待运行 with the bar pinned at zero.
  const queued = run.status === "pending" && !run.started_at;
  const live = running && !failed && !queued;
  // Either the request is still in flight or the round has been asked for it. Both read the
  // same on the button, and both end in the same place: the row leaving the live states.
  const stopping = cancelling || asked;
  const stop = () => {
    setAsked(true);
    // The request itself can still fail - the API can be down - and a button stuck on
    // 正在中止 is a stop nobody can press again.
    onCancel().catch(() => setAsked(false));
  };
  const active = ACTIVE_STAGE[run.status] ?? 0;
  const reached = failed || queued ? 0 : running ? active : STAGES.length;
  // The pipeline's own percent, and the stage it belongs to. A status is a boundary, not a
  // position: without this the bar would sit at one sixth for the minutes the filter spends
  // reading the pool. The stage count stays as a floor so the bar cannot go backwards
  // against the state the round is already in.
  const reported = typeof run.progress === "number" ? run.progress : null;
  const stageProgress = Math.min(1, reached / STAGES.length);
  const progress = failed || queued
    ? 0
    : live
      ? Math.max(stageProgress, (reported ?? 0) / 100)
      : 1;

  const sources = batch?.sources ?? [];
  // One 淘汰 row, and it is the strip's own number: every rule that can reject a token does
  // it before the round spends a token on it, so the filter's share and the total are one
  // count. `run.dropped` is that count and it is the one the strip reads, so the two can
  // never disagree - including on the rounds archived before the split was removed, whose
  // `dropped_hard` is only the pool rules' share of their own total.
  const hardDropped = run.dropped ?? batch?.dropped_hard ?? 0;

  const result = (key: string): string => {
    if (key === "collect") {
      if (!sources.length) {
        return run.scanned ? `${run.scanned.toLocaleString()} 个` : "—";
      }
      return sources
        .map((source) => `${SOURCE_SHORT[source.name] ?? source.name} ${source.count.toLocaleString()}`)
        .join(" · ");
    }
    if (key === "dedupe") {
      return run.scanned ? `→ ${run.scanned.toLocaleString()}` : "—";
    }
    if (key === "prefilter") {
      return hardDropped ? `→ 淘汰 ${hardDropped.toLocaleString()}` : "—";
    }
    if (key === "score") {
      return run.candidates ? `→ 候选 ${run.candidates}` : "—";
    }
    if (key === "report") {
      return run.selected ? `→ 入选 ${run.selected}` : "—";
    }
    if (key === "push") {
      const status = batch?.push.status;
      return status ? `→ ${status}` : "—";
    }
    return "—";
  };

  const timings = batch?.phase_timings ?? {};
  // A phase that ended has a number, even when the number is zero: a stage that took a
  // tenth of a second still ran, and a dash there would claim it never did. Only a row
  // whose phases are all still missing draws the dash, the same way its result does.
  const took = (key: string): string => {
    const ended = (STAGE_PHASES[key] ?? []).filter((phase) => timings[phase] !== undefined);
    if (!ended.length) {
      return "—";
    }
    return mmss(Math.round(ended.reduce((total, phase) => total + timings[phase], 0)));
  };

  // A round that failed records no progress, so the row it stopped on is the first one whose
  // phases have no stopwatch reading - the same boundary `reached` reads while it is alive.
  const failedAt = failed
    ? STAGES.findIndex((stage) =>
        (STAGE_PHASES[stage.key] ?? []).every((phase) => timings[phase] === undefined),
      )
    : -1;
  const marker = (index: number): "pending" | "run" | "done" | "failed" => {
    if (index < reached) return "done";
    if (live && index === reached) return "run";
    if (index === failedAt) return "failed";
    return "pending";
  };

  const scanned = (run.scanned ?? 0).toLocaleString();
  const picked = run.selected ?? batch?.funnel.selected ?? 0;
  const foot = failed
    ? `扫描全量候选池 ${scanned} 个标的 · 本轮失败，未产出报告 · ${run.error ?? "未记录原因"}`
    : cancelled
      ? `扫描全量候选池 ${scanned} 个标的 · 本轮已中止，未产出报告`
        : queued
          ? `本轮待运行 · 跑完自动落库`
          : running
            ? `扫描全量候选池 ${scanned} 个标的 · 产出 ${picked} 份报告 · 跑完自动落库`
            : `扫描 ${scanned} → 候选 ${run.candidates ?? 0} → 入选 ${picked} · 已生成 ${picked} 份报告 · 阶段结果取数据库里的真实状态`;

  return (
    <div
      className="runpanel"
      id="runpanel"
      hidden={!open}
    >
      <div className="rp-h">
        <b id="rp-title">
          {failed
            ? "本轮失败"
            : cancelled
              ? "已中止 · 本轮"
              : queued
                ? "本轮 · 待运行"
                : running
                  ? "正在运行 · 本轮"
                  : "运行完成 · 本轮"}
        </b>
        <span className="rp-el" id="rp-el">
          {running || queued ? `已用 ${elapsed(run, true, now)}` : `用时 ${elapsed(run, false, now)}`}
        </span>
        {!queued && reported !== null ? (
          <span className="rp-el" id="rp-pct">
            {reported}%{run.progress_label ? ` · ${run.progress_label}` : ""}
          </span>
        ) : null}
        <span className="spacer" />
        {queued || live ? (
          stopping ? (
            <span className="btn sec demo" id="rp-abort">
              <Icon as={LoaderCircle} className="spin" size={14} />
              正在中止
            </span>
          ) : (
            <button
              className={armed ? "btn sec arm" : "btn sec"}
              id="rp-abort"
              onClick={armed ? stop : () => setArmed(true)}
              title={
                armed
                  ? "再点一次：本轮立刻停在当前阶段，本轮不产出报告"
                  : "停止这一轮：本轮不产出报告，已经跑过的阶段留在记录里"
              }
            >
              <Icon as={Pause} size={14} />
              {armed ? "确认中止" : "中止"}
            </button>
          )
        ) : null}
      </div>
      <div className="rp-bar">
        <i id="rp-fill" style={{ transform: `scaleX(${progress.toFixed(4)})` }} />
      </div>
      <ol className="rp-steps" id="rp-steps">
        {STAGES.map((stage, index) => (
          <li
            className={
              queued ? "rst" : `rst ${index === failedAt ? "failed" : marker(index)}`
            }
            key={stage.key}
          >
            <Marker state={marker(index)} />
            <b>{stage.label}</b>
            <span className="rst-r">{result(stage.key)}</span>
            <em className="rst-t">{took(stage.key)}</em>
          </li>
        ))}
      </ol>
      <div className="rp-foot" id="rp-foot">
        {foot}
      </div>
    </div>
  );
}
