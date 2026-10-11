import { ArcMark } from "./ArcMark";
import { BscMark } from "./BscMark";
import { RobinhoodMark } from "./RobinhoodMark";
import { SolanaMark } from "./SolanaMark";

/** Each chain can carry its own mark - the chain's own artwork, not one of ours. Arc,
 *  BSC, Robinhood and Solana have one; a chain without a mark is the label alone. Kept here
 *  rather than in the chain payload: the API
 *  answers what a chain *is* (id, label, enabled), not how this screen draws it.
 *
 *  One function for every list that names a source (2026-10-02, user): the rail's chain rows
 *  and the 收藏 page's 来源 rows open with the same glyph, so the two cannot disagree about
 *  which mark a chain wears. */
export function chainIdMark(id: string) {
  if (id === "arc") {
    return <ArcMark size={14} />;
  }
  if (id === "bsc") {
    return <BscMark size={14} />;
  }
  if (id === "robinhood") {
    return <RobinhoodMark size={14} />;
  }
  if (id === "solana") {
    return <SolanaMark size={14} />;
  }
  return null;
}
