import { Compass, Moon, Search, Settings, Star, Sun, Telescope } from "lucide-react";

import type { Chain } from "../../api/client";
import { Icon } from "../../components/Icon";
import { Mark } from "../../components/Mark";
import { chainIdMark } from "../../components/chainMark";
import type { Theme } from "../../theme";

/** The places this page has. 雷达 is per chain and carries 报告 / 设定; 收藏 is one shelf for
 *  every chain; 搜索 reads one address from any chain; 新机会 is what the outside world is
 *  offering, found by this app's own scans; 系统设置 is this machine - the gateway, the X
 *  session, the log level. None of the last four is a row in the chain list: a kept card, a
 *  searched one, an activity and a gateway's key can all belong to something this app never
 *  screens. */
export type Place = "radar" | "favorites" | "lookup" | "opportunity" | "monitors" | "system";

/** The rail. Only the chains that are wired, as plain names: the product is one radar per
 *  chain, not a menu, and a row nobody can open is not a menu item. A chain's 设定 is a tab in
 *  the topbar, not a rail entry - but 系统设置 is one, because it is not a chain's to own. */

export function Rail({
  chains,
  activeChainId,
  onSelectChain,
  place,
  onSelectPlace,
  theme,
  onTheme,
}: {
  chains: Chain[] | undefined;
  activeChainId: string;
  onSelectChain: (chainId: string) => void;
  /** Which of the two places the page is in. 收藏 is not a radar and not a chain. */
  place: Place;
  onSelectPlace: (place: Place) => void;
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
        {/* 新机会 sits above the radars: it reads the outside world rather than a chain the
            reader chose, so it is not part of the chain list (user, 2026-10-21). */}
        <div
          className={"nav-i" + (place === "opportunity" ? " on" : "")}
          onClick={() => onSelectPlace("opportunity")}
        >
          <Icon as={Compass} size={14} />
          新机会
        </div>
        {/* 监控 sits under 新机会 because it is the same instinct read one level down: not "what
            is new", but "answer this question again" (user, 2026-10-29). */}
        <div
          className={"nav-i" + (place === "monitors" ? " on" : "")}
          onClick={() => onSelectPlace("monitors")}
        >
          <Icon as={Telescope} size={14} />
          监控
        </div>
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
        {/* The one row that is neither a chain nor a shelf: this machine's own wiring. It sits
            at the bottom of the list because it is global, like the theme switch below it, and
            it is a Place all the same - so exactly one row lights and it can be left
            for on a reload (prefs). */}
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
