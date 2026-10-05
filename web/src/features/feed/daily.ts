import type { FeedEditionRow } from "../../api/client";

/** 三种刊. 这三个词是**后端**那一侧的键 (`service.KINDS`), 不是页面的排版用词, 所以这里
 *  照抄, 不另起一套名字. */
export type FeedKind = "day" | "week" | "month";

/** 这一页认得的"某一期": 列表里的一行和整页的正文都有这几个字段, 而下面这些函数只该问它们
 *  要这几样 —— 写成 `FeedEditionRow` 会让正文那一份类型对不上 (正文的 `entries` 是条目,
 *  列表的 `entries` 是条数, 同一个名字两种东西). */
export type Issue = Pick<FeedEditionRow, "id" | "key" | "window_from" | "window_to" | "headline">;

const HOUR = 3600 * 1000;
const WEEK = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
const MONTH = ["一月", "二月", "三月", "四月", "五月", "六月",
               "七月", "八月", "九月", "十月", "十一月", "十二月"];

/** 后端发的是带时区的 ISO, 而"10 月 3 日"是 +08 的那一天 —— 一期覆盖哪一段, 是这一页的
 *  正文数据(不是"多久以前"那种相对词), 所以这里是全项目唯一一处页面自己算时间的地方.
 *  加 8 小时再一律按 UTC 读, 等价于按 +08 读, 又不随读者所在时区变. */
function at8(iso: string): Date {
  return new Date(new Date(iso).getTime() + 8 * HOUR);
}

/** 窗口尾端是**不含**的那一头, 所以"覆盖到哪一天"要减一毫秒再看. */
function until8(iso: string): Date {
  return new Date(new Date(iso).getTime() - 1 + 8 * HOUR);
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function mmdd(at: Date): string {
  return `${pad(at.getUTCMonth() + 1)}.${pad(at.getUTCDate())}`;
}

export function weekdayText(iso: string): string {
  return WEEK[at8(iso).getUTCDay()];
}

export function clockText(iso: string): string {
  const at = at8(iso);
  return `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}`;
}

/** 档案栏那一行左侧的大字: 日报是几号, 周报是第几周, 月报是几月.
 *
 *  从 `key` 里取, 不从时间戳里算 —— ISO 周号不是能从日期"看"出来的东西, 后端已经给了就
 *  不该在页面再实现一遍 (CLAUDE.md 2.3.3). */
export function markOf(kind: FeedKind, key: string): string {
  if (kind === "day") return key.slice(8, 10);
  if (kind === "week") return key.slice(6);
  return key.slice(5, 7);
}

/** 档案栏那一行大字旁边的小字. 日报印星期几 (读者按"上周六"找一期), 周报印它从哪天起,
 *  月报不印 —— 年份在分组头上, 月份就是那个大字本身. */
export function metaOf(kind: FeedKind, row: Issue): string {
  if (kind === "day") return weekdayText(row.window_from);
  if (kind === "week") return `${mmdd(at8(row.window_from))} 起`;
  return "";
}

/** 报头: 大字 + 两行小字. 两行取的都是**这一期覆盖的那一段**, 不是"什么时候出的刊". */
export function masthead(kind: FeedKind, row: Issue):
    { mark: string; line1: string; line2: string } {
  const year = row.key.slice(0, 4);
  const month = Number(row.key.slice(5, 7));
  if (kind === "day") {
    return { mark: markOf(kind, row.key), line1: `${year} 年 ${month} 月`,
             line2: weekdayText(row.window_from) };
  }
  if (kind === "week") {
    return { mark: markOf(kind, row.key), line1: `${year} 年第 ${Number(row.key.slice(6))} 周`,
             line2: periodText(kind, row) };
  }
  return { mark: markOf(kind, row.key), line1: `${year} 年`, line2: MONTH[month - 1] };
}

/** 一期写在一行里: 日报"10 月 3 日", 周报"09.28 — 10.04", 月报"2026 年 10 月".
 *  相邻期那个按钮上用的是它 —— "上一期 · 9月25日" 比 "上一期" 有用得多. */
export function periodText(kind: FeedKind, row: Issue): string {
  const year = row.key.slice(0, 4);
  if (kind === "day") return `${Number(row.key.slice(5, 7))} 月 ${Number(row.key.slice(8, 10))} 日`;
  if (kind === "week") return `${mmdd(at8(row.window_from))} — ${mmdd(until8(row.window_to))}`;
  return `${year} 年 ${Number(row.key.slice(5, 7))} 月`;
}

/** 档案栏那一行右侧的标题. 空的一期也印一句话, 不印一个空白. */
export function headlineOf(row: Issue): string {
  return row.headline || "这一期没有值得上刊的事";
}

/** 档案栏的分期: 日报按月, 周报和月报按年. 再细一层就不是"翻旧报纸"了, 是"查档案". */
export function archiveGroups(kind: FeedKind, rows: Issue[], limit: number):
    { label: string; rows: Issue[]; complete: boolean }[] {
  const groups: { label: string; rows: Issue[]; complete: boolean }[] = [];
  for (const row of rows) {
    const label = groupLabel(kind, row.key);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.rows.push(row);
    else groups.push({ label, rows: [row], complete: false });
  }
  // `complete` = "这一组的期数是不是全部在手". 列表只加载最近几十期, 最旧的那一组可能被
  // 截断, 而截断的一组说出"本月 3 期"是错的. 行是从新到旧连续的, 所以只要下面还有更旧的一组,
  // 或者这一批本来就没取满, 这一组就是完整的.
  groups.forEach((group, index) => {
    group.complete = index > 0 || rows.length < limit;
  });
  return groups;
}

function groupLabel(kind: FeedKind, key: string): string {
  if (kind === "day") return `${key.slice(0, 4)} 年 ${Number(key.slice(5, 7))} 月`;
  return `${key.slice(0, 4)} 年`;
}

/** 报眼的点阵: 这一期所在的那一级一共几格, 哪几格出过刊.
 *
 *  格子跟刊走: 日报一天一格 (按星期对齐, 因为读者认的是"上周三"), 周报一周一格, 月报一月
 *  一格. 底下那句数的是**已经加载进来的**那些格 —— 跟 `archiveGroups` 同一个口径, 而且只有
 *  在完整的那一组上才印得准, 所以它跟 `complete` 绑在一起由调用方决定印不印. */
export function dots(kind: FeedKind, row: Issue, rows: Issue[]):
    { cells: { label: string; on: boolean }[]; columns: number; caption: string; first: number } {
  const keys = new Set(rows.map((each) => each.key));
  const year = Number(row.key.slice(0, 4));
  if (kind === "day") {
    const month = Number(row.key.slice(5, 7));
    const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const cells = Array.from({ length: days }, (_, index) => {
      const day = index + 1;
      return { label: `${month} 月 ${day} 日`,
               on: keys.has(`${year}-${pad(month)}-${pad(day)}`) };
    });
    const shown = cells.filter((cell) => cell.on).length;
    // 日历的样子: 第一格前面空出几天, 让 1 号落在它那个星期几上.
    return { cells, columns: 7, first: new Date(Date.UTC(year, month - 1, 1)).getUTCDay(),
             caption: `本月 ${shown} 期` };
  }
  const cells = kind === "week"
    ? Array.from({ length: 53 }, (_, index) => ({
        label: `第 ${index + 1} 周`, on: keys.has(`${year}-W${pad(index + 1)}`) }))
    : Array.from({ length: 12 }, (_, index) => ({
        label: `${index + 1} 月`, on: keys.has(`${year}-${pad(index + 1)}`) }));
  const shown = cells.filter((cell) => cell.on).length;
  return { cells, columns: kind === "week" ? 13 : 6, first: 0, caption: `全年 ${shown} 期` };
}
