import { Book, Flame, List, Moon, Search, Settings, SlidersHorizontal, Star, Sun, Tag, Zap } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { Chain } from "../../api/client";
import { Icon } from "../../components/Icon";
import { Mark } from "../../components/Mark";
import { chainIdMark } from "../../components/chainMark";
import type { Theme } from "../../theme";
import type { FeedMode } from "../feed/FeedView";

/** The places this page has. 雷达 is per chain and carries 报告 / 设定; 收藏 is one shelf for
 *  every chain; 搜索 reads one address from any chain; 信息流 is the second product - the
 *  industry itself rather than one chain's tokens; 信息流设定 is where that product's own clocks
 *  and budgets live; 系统设置 is this machine. None of the last five is a row in the chain list:
 *  a kept card, a searched one, an industry item, an issue's schedule and a gateway's key can
 *  all belong to something this app never screens. */
export type Place = "radar" | "feed" | "favorites" | "lookup" | "system" | "feed-settings";

/** 信息流的五页, 排成左栏的一组. 四个名字与四个图标是版式, 所以在这里而不是从后端来: 后端
 *  发的是每一条内容, 不是这一页叫什么. The mode is the backend's key; the word is this rail's.
 *  顺序挨着分了组: 精选与全部动态是"谁在什么时候发了一条" (报道), 热点榜与主题是"这件事 /
 *  这条线现在什么样" (事件与追踪对象), 报刊是"那一段时期出过的那一期" —— 它读的是出过的刊而
 *  不是当前窗口, 所以它在最后. 日报 / 周报 / 月报合成它一页, 切换器在页内 (用户定的, 2026-10-03): 那是同一
 *  张报纸的三个周期, 在导航里占三行等于说它们是三个地方, 而这一栏的规矩是"哪一页由 Rail 决定"
 *  —— 换的是版期, 不是页, 所以这条规矩没破. */
const FEED_ROWS: { mode: FeedMode; label: string; icon: LucideIcon }[] = [
  { mode: "curated", label: "精选", icon: Zap },
  { mode: "all", label: "全部动态", icon: List },
  { mode: "hot", label: "热点榜", icon: Flame },
  { mode: "topics", label: "主题", icon: Tag },
  { mode: "papers", label: "报刊", icon: Book },
];

/** The rail. Only the chains that are wired, as plain names: the product is one radar per
 *  chain, not a menu, and a row nobody can open is not a menu item. */

export function Rail({
  chains,
  activeChainId,
  onSelectChain,
  place,
  onSelectPlace,
  feedMode,
  onSelectFeed,
  theme,
  onTheme,
}: {
  chains: Chain[] | undefined;
  activeChainId: string;
  onSelectChain: (chainId: string) => void;
  /** Which of the places the page is in. 收藏 and 搜索 are not a radar and not a chain. */
  place: Place;
  onSelectPlace: (place: Place) => void;
  /** 信息流 是三个页面而不是一个, 所以它比别的地方多一个维度: 在哪一页. Kept apart from
   *  `place` because it is only meaningful while the place is 信息流 - a mode carried beside
   *  a place would be a value that is sometimes about nowhere. */
  feedMode: FeedMode;
  onSelectFeed: (mode: FeedMode) => void;
  theme: Theme;
  onTheme: (theme: Theme) => void;
}) {
  return (
    <aside className="rail">
      <div className="brand">
        <Icon as={Mark} size={14} />
        <b>CRYPTO HUNTER</b>
      </div>
      <nav className="nav">
        {/* Only chains that are wired get a row. A chain nobody can open is not a menu
            item: 待接 on BSC and a 添加链 待定 placeholder both raised a question the rail
            cannot answer, so they are gone (user, 2026-09-26). A chain carries no count
            either - the rail says which radar you are in, and the round's own numbers
            are on the round's strip. */}
        {(chains ?? []).filter((chain) => chain.enabled).map((chain) => {
          // One lit row at a time: a chain is the place you are in only while a radar is the
          // place you are in - on the shelf the chain you last read is not where you are.
          const on = chain.id === activeChainId && place === "radar";
          return (
            <div
              key={chain.id}
              className={`nav-i${on ? " on" : ""}`}
              onClick={() => onSelectChain(chain.id)}
            >
              {chainIdMark(chain.id)}
              {chain.label}
            </div>
          );
        })}
        {/* The two places that are not a radar, right under the radars: a kept card and a
            searched one both belong to no single chain, so neither is a row in a list of
            chains - but they sit in the radars' own list, not on a shelf apart from it.
            搜索 leads: looking a CA up is the way in, and the shelf is what it leaves behind
            (user, 2026-10-01). */}
        <div className="nav-shelf">
          <div
            className={"nav-i" + (place === "lookup" ? " on" : "")}
            onClick={() => onSelectPlace("lookup")}
          >
            <Icon as={Search} size={14} />
            搜索
          </div>
          <div
            className={"nav-i" + (place === "favorites" ? " on" : "")}
            onClick={() => onSelectPlace("favorites")}
          >
            <Icon as={Star} size={14} />
            收藏
          </div>
        </div>
        {/* 信息流 is the second product, so it is a group of its own rather than one more row:
            one heading and its four pages, the shape the industry reader arrives in. It sits
            below the shelf because it is not a radar and not a token, and a step above
            系统设置 because it is content rather than this machine's own wiring. */}
        <div className="nav-sec">信息流</div>
        {FEED_ROWS.map((row) => (
          <div
            key={row.mode}
            className={"nav-i" + (place === "feed" && feedMode === row.mode ? " on" : "")}
            onClick={() => onSelectFeed(row.mode)}
          >
            <Icon as={row.icon} size={14} />
            {row.label}
          </div>
        ))}
        {/* 信息流设定 是这一栏的第六行, 不是第六页 (用户定的, 2026-10-05): 出刊节奏和两道闸门
            不属于上面任何一页, 所以它不能是它们中间的一个. 它是一行而不是 信息流 底下的一个 tab,
            因为这一栏的规矩是「哪一页由 Rail 决定」—— 而它不是那一页的版期. */}
        <div
          className={"nav-i" + (place === "feed-settings" ? " on" : "")}
          onClick={() => onSelectPlace("feed-settings")}
        >
          <Icon as={SlidersHorizontal} size={14} />
          信息流设定
        </div>
        {/* 系统设置 is a rail row because it belongs to no chain (user, 2026-10-02), and it is
            pinned under everything else: the chains, the shelf and the feed are things you
            read, and this is the machine they read with - the theme switch below it is the
            other global row. .nav is a flex *item*, so margin-top:auto on a child of it does
            nothing until the list itself is a column flex box - that pair is the whole rule,
            and a rail with three chains and a rail with one both put this row at the bottom. */}
        <div className="nav-foot">
          <div
            className={"nav-i" + (place === "system" ? " on" : "")}
            onClick={() => onSelectPlace("system")}
          >
            <Icon as={Settings} size={14} />
            系统设置
          </div>
        </div>
      </nav>
      {/* Two segments, not one toggle: with two states a reader should see which one they
          are in rather than guess what the next click does. */}
      <div className="rail-foot">
        <div className="theme-sw" role="group" aria-label="主题">
          {(["dark", "light"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={theme === value ? "on" : undefined}
              aria-pressed={theme === value}
              onClick={() => onTheme(value)}
            >
              <Icon as={value === "dark" ? Moon : Sun} />
              {value === "dark" ? "深色" : "浅色"}
            </button>
          ))}
        </div>
      </div>
    </aside>
  );
}
