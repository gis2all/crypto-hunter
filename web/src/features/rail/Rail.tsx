import { Moon, Star, Sun } from "lucide-react";

import type { Chain } from "../../api/client";
import { ArcMark } from "../../components/ArcMark";
import { RobinhoodMark } from "../../components/RobinhoodMark";
import { Icon } from "../../components/Icon";
import { Mark } from "../../components/Mark";
import type { Theme } from "../../theme";

/** Each chain can carry its own mark - the chain's own artwork, not one of ours. Arc and
 *  Robinhood have one; a chain without a mark is the label alone, which is what BSC will be
 *  until its artwork is in. Kept here rather than in the chain payload: the API answers what a
 *  chain *is* (id, label, enabled), not how this screen draws it. */
function chainIdMark(id: string) {
  if (id === "arc") {
    return <ArcMark size={14} />;
  }
  if (id === "robinhood") {
    return <RobinhoodMark size={14} />;
  }
  return null;
}

/** The two places this page has. 雷达 is per chain and carries 报告 / 设定; 收藏 is
 *  one shelf for every chain, so it is not a row in the chain list - the shelf can hold a
 *  card from a chain this app does not screen at all. */
export type Place = "radar" | "favorites";

/** The rail. Only the chains that are wired, as plain names: the product is one radar per
 *  chain, not a menu, and a row nobody can open is not a menu item. Settings is a tab in
 *  the topbar, not a rail entry. */

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
      </nav>
      {/* The shelf, under the radars and separated from them: a kept card belongs to no
          single chain, so it is not a row in a list of chains - it is the other place. */}
      <nav className="nav-shelf">
        <div
          className={"nav-i" + (place === "favorites" ? " on" : "")}
          onClick={() => onSelectPlace("favorites")}
        >
          <Icon as={Star} size={14} />
          收藏
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
