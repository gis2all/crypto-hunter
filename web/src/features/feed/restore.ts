/** 列表页的位置快照: 读过的那几批、哪几天折着、停在屏幕上的是哪一条 (AIHOT 的 `restore.ts`).
 *
 *  为什么不是"每次重开都回到上次读到的地方": 一个滚到一半的旧列表会盖住列表顶上那几条新的,
 *  而读者重开页面要的正是"现在有什么" (AIHOT 同一条规矩: 由 reload 打开的那一次不恢复)。
 *  所以快照只在**同一会话里换页再回来**时生效 —— 换一条链、换一个视图、再切回来, 读到哪还在哪;
 *  按 F5 就当重新来过。
 *
 *  存在 sessionStorage: 它是浏览器的会话, 不是服务端的事实, 也不该跨会话留着 (prefs.ts 那些
 *  选择记在 localStorage, 因为它们跨会话仍然对; 位置不是)。
 */

const PREFIX = "ch.feed.list.";
//: 快照的寿命. 超过它的那份东西已经不知道说的是哪个列表了 —— 窗口在移, 条目在加.
const MAX_AGE_MS = 30 * 60 * 1000;

export interface ListSnapshot<T> {
  savedAt: number;
  data: T;
  anchor: { key: string; offset: number } | null;
}

/** 这一份文档是被**刷新**打开的. 刷新要的是最新的列表, 所以它不恢复快照 (AIHOT 的 `isReload`)。 */
export function isReload(): boolean {
  try {
    const entry = performance.getEntriesByType("navigation")[0] as
      PerformanceNavigationTiming | undefined;
    return entry?.type === "reload";
  } catch {
    return false;
  }
}

export function readSnapshot<T>(key: string): ListSnapshot<T> | null {
  try {
    const raw = sessionStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const snap = JSON.parse(raw) as ListSnapshot<T>;
    if (!snap || !Number.isFinite(snap.savedAt) || Date.now() - snap.savedAt > MAX_AGE_MS) {
      sessionStorage.removeItem(PREFIX + key);
      return null;
    }
    return snap;
  } catch {
    // 存储不可用 (隐私模式) 就当没有快照: 列表从第一页开始, 和第一次进来一样.
    return null;
  }
}

/** 停下来那一刻, 屏幕上最靠上的一条是哪一条 (锚点). 找不到就给 null. */
export function currentAnchor(selector = "[data-card-key]"): ListSnapshot<unknown>["anchor"] {
  try {
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(selector))) {
      if (el.getBoundingClientRect().bottom > 72) {
        return { key: el.dataset.cardKey ?? "", offset: el.getBoundingClientRect().top };
      }
    }
  } catch {
    // 没有 DOM 就没有锚点.
  }
  return null;
}

export function saveSnapshot<T>(key: string, data: T, selector = "[data-card-key]"): void {
  try {
    const snap: ListSnapshot<T> = { savedAt: Date.now(), data, anchor: currentAnchor(selector) };
    sessionStorage.setItem(PREFIX + key, JSON.stringify(snap));
  } catch {
    // 写不进去就不写: 位置是便利, 不是数据.
  }
}

/** 把那一条放回它上次所在的屏幕位置.
 *
 *  找不到那一条就**什么都不做**, 而不是退回去滚一个绝对像素: 列表短了 (只加载了第一页) 的时候,
 *  一个原来合法的 scrollY 会把读者丢到空白处 —— 停在顶上至少是"从头看"这个能解释的状态。
 */
export function restoreAnchor(anchor: ListSnapshot<unknown>["anchor"]): void {
  if (!anchor || !anchor.key) return;
  const apply = () => {
    const el = document.querySelector<HTMLElement>(`[data-card-key="${CSS.escape(anchor.key)}"]`);
    if (!el) return;
    window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - anchor.offset,
                      behavior: "instant" as ScrollBehavior });
  };
  requestAnimationFrame(() => {
    apply();
    // 图与晚到的版式会让内容再挪一次, 所以稳一帧之后再对一次.
    setTimeout(apply, 120);
  });
}
