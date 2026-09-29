import { ChevronRight, Download, FileText, FoldVertical, Layers, LayoutGrid, Play, Save, Send, Settings2, SlidersHorizontal, UnfoldVertical } from "lucide-react";

import type { Batch, Chain } from "../../api/client";
import { Icon } from "../../components/Icon";
import { Pick } from "../../components/Pick";
import { whole } from "../../format";
import type { SettingsState } from "../settings/useSettingsState";

export type View = "report" | "params";

/** The right-hand end of the topbar is the actions of the surface you are standing on,
 *  never one fixed row of four: 报告 keeps the round's own controls, 设定 swaps them for
 *  the two buttons that act on that page. The same slots therefore hold the only controls
 *  that mean anything where you are - 本轮总览 / 导出 / 推送 TG are readings of a round and
 *  make no sense beside a form, and 保存并生效 / 立即运行 act on the form and nothing else. */
export function TopBar({
  view,
  onView,
  onOverview,
  onExport,
  settings,
  chain,
  configId,
  onSelectConfig,
  onManage,
  openAll,
  onToggleCards,
}: {
  view: View;
  onView: (view: View) => void;
  onOverview: () => void;
  onExport: () => void;
  settings: SettingsState;
  chain: Chain | undefined;
  configId: string;
  onSelectConfig: (configId: string) => void;
  /** Open the drawer that adds, renames and drops sets of settings. */
  onManage: () => void;
  openAll: boolean;
  onToggleCards: () => void;
}) {
  const configs = chain?.configs ?? [];
  const current = configs.find((item) => item.id === configId);

  return (
    <header className="topbar">
      {/* No chain name here: the rail on the left already says which radar this is, and a
          second copy beside the tabs reads as a third tab that does nothing. */}
      <div className="tabs">
        <span
          className={`tab${view === "report" ? " on" : ""}`}
          onClick={() => onView("report")}
        >
          <Icon as={FileText} />
          报告
        </span>
        <span className={`tab${view === "params" ? " on" : ""}`} onClick={() => onView("params")}>
          <Icon as={SlidersHorizontal} />
          设定
        </span>
      </div>
      <div className="acts">
        {view === "report" ? (
          <>
            {/* One label, two states: the same control opens every card or closes every
                card, so the reader learns it once and it always says what the next click
                does. */}
            <button className="btn sec" onClick={onToggleCards}>
              <Icon as={openAll ? FoldVertical : UnfoldVertical} size={14} />
              {openAll ? "全部收起" : "全部展开"}
            </button>
            <button className="btn sec" onClick={onOverview}>
              <Icon as={LayoutGrid} size={14} />
              本轮总览
              <Icon as={ChevronRight} size={14} />
            </button>
            {/* Two gates, not two features: neither is on the pipeline yet, and a control
                that looks pressable and does nothing costs the reader a click. They carry
                the frozen design's own demo treatment (`btn ... demo`: dimmed, no hover,
                not pressable) plus a real `disabled`, so the look and the behaviour say
                the same thing. The handler stays wired for the day the export lands. */}
            <button
              className="btn sec demo"
              disabled
              title="导出尚未接入（当前不做任何事）"
              onClick={onExport}
            >
              <Icon as={Download} size={14} />
              导出
            </button>
            <button className="btn sec demo" disabled title="TG 推送尚未接入">
              <Icon as={Send} size={14} />
              推送 TG
            </button>
          </>
        ) : (
          <>
            {/* Which set of settings this page is editing, and the way to the other three
                writes. Both stand left of 保存并生效 because they are what the two buttons
                on their right act on: 保存并生效 writes the set the picker names and
                立即运行 runs it - the row reads left to right as 这一套 → 全部几套 → 写 → 跑
                (user, 2026-09-29; moved here from the middle the same day). The list it
                opens is drawn by the app, not by the platform - see components/Pick.tsx. */}
            {configs.length ? (
              <Pick
                className="btn sec cfg-pick"
                label="配置"
                current={configId}
                trigger={
                  <>
                    <Icon as={Layers} size={14} />
                    <span className="cfg-cur">
                      {current ? (current.enabled ? `${current.label} · 生效` : current.label) : ""}
                    </span>
                  </>
                }
                options={configs.map((config) => ({
                  value: config.id,
                  label: (
                    <>
                      <span className={`cfg-dot${config.enabled ? " live" : ""}`} />
                      {config.label}
                      {config.enabled ? <em className="cfg-tag on">生效</em> : null}
                    </>
                  ),
                }))}
                onPick={onSelectConfig}
              />
            ) : null}
            {/* 配置管理 and 本轮总览 open the same way - a drawer over the page - so they
                end the same way: one trailing chevron, the app's own mark for "this one
                leaves the page". */}
            <button className="btn sec" onClick={onManage}>
              <Icon as={Settings2} size={14} />
              配置管理
              <Icon as={ChevronRight} size={14} />
            </button>
            <button
              className="btn sec"
              id="save-btn"
              onClick={() => void settings.commit()}
              disabled={settings.save.isPending}
            >
              <Icon as={Save} size={14} />
              保存并生效
            </button>
            <button
              className="btn"
              id="run-btn"
              disabled={settings.runNow.isPending || settings.save.isPending}
              onClick={() => {
                void settings.commit().then((saved) => {
                  // The run only starts on settings that saved; a refused save is already
                  // on screen above the form, and running under it would lie.
                  if (saved) settings.runNow.mutate();
                });
              }}
            >
              <Icon as={Play} size={14} />
              立即运行
            </button>
          </>
        )}
      </div>
    </header>
  );
}

/** Markdown of the round exactly as drawn. The export is what is on screen, not a
 *  second rendering path that could disagree with it. */
export function batchToMarkdown(batch: Batch): string {
  const lines: string[] = [
    `# Crypto Hunter · ${batch.batch_label}`,
    "",
    `扫描 ${batch.funnel.scanned.toLocaleString()} → 候选 ${batch.funnel.candidates} → 入选 ${batch.funnel.selected}`,
    "",
  ];
  if (batch.changes.length) {
    lines.push("## 本期变化", "");
    for (const change of batch.changes) {
      lines.push(`- ${change.kind}：${change.detail}`);
    }
    lines.push("");
  }
  for (const card of batch.cards) {
    lines.push(`## ${String(card.rank).padStart(2, "0")} ${card.sym} · 综合分 ${whole(card.score)}`);
    lines.push("");
    lines.push(`\`${card.ca}\``);
    lines.push("");
    lines.push(card.verdict.map((segment) => segment.text).join(""));
    lines.push("");
    lines.push("| 指标 | 值 |");
    lines.push("| --- | --- |");
    for (const metric of card.metrics) {
      lines.push(`| ${metric.key} | ${metric.value} |`);
    }
    lines.push("");
    for (const detail of card.details) {
      lines.push(`### ${detail.title}`);
      lines.push("");
      lines.push(detail.body.map((segment) => segment.text).join(""));
      lines.push("");
    }
  }
  return lines.join("\n");
}
