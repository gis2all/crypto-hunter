import { useCallback, useState } from "react";

import { readPref, savePref } from "../../prefs";

/** 读过的那几条, 记在本地.
 *
 *  它只管一件事: 标题读过了就淡下去, 让读者一眼看出"这行我看过". 所以它是浏览器的事实,
 *  不是服务端的事实 —— 没有账号, 没有回执, 也不该为它多一条写请求. 换个浏览器就是没读过,
 *  那正是"我的已读"的意思.
 *
 *  只留最近 CAP 条: 这是一个提示, 不是一个档案. 满了就从最早的丢, 于是这个键不会长到
 *  让每次读页都变慢, 也不会让 localStorage 顶到配额.
 */

const KEY = "ch.feed.read";
const CAP = 400;

function load(): string[] {
  try {
    const raw = readPref(KEY);
    const held = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(held) ? held.filter((id): id is string => typeof id === "string") : [];
  } catch {
    // 存坏的值 = 没有记录, 不是页面挂掉.
    return [];
  }
}

function stash(ids: string[]): void {
  savePref(KEY, JSON.stringify(ids.slice(-CAP)));
}

export function useReadSet(): { read: Set<string>; mark: (id: string) => void } {
  const [read, setRead] = useState<Set<string>>(() => new Set(load()));
  const mark = useCallback((id: string) => {
    setRead((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      stash([...next]);
      return next;
    });
  }, []);
  return { read, mark };
}
