import { useState } from "react";

/** The token's own mark, in the box a head keeps for it.
 *
 *  The box is drawn whether or not there is a picture in it: a card with no mark is the same
 *  card with nothing in that box, not a card of a different height - the same reason the
 *  shelf reserves the score's corner (8.8.2). It is a square because the whole screen is, and
 *  the fill when it is empty is the theme's own black or white (--logo-empty) rather than
 *  nothing at all, so an empty box reads as empty. The box is drawn in the accent at 2px, and
 *  with a 6px radius (2026-10-01, user): the fill says which box it is, and the ring says
 *  there is a box at all - a hairline in --border was too quiet on the light theme, where the
 *  empty fill is white on a near-white card.
 *
 *  On a card it is the height of the two lines beside it - the symbol and the address, which
 *  the head is - so the head stands or falls on the text, and a mark can be any shape it likes
 *  inside the square without moving anything.
 *
 *  A mark that will not load leaves the box as it stands instead of putting a broken-image
 *  glyph in the middle of a card: the box already says "no picture", and a failed fetch has
 *  nothing to add to it.
 *
 *  The size is the caller's (2026-10-02, user): a card head keeps a 40px box and the 最近搜索
 *  row draws the same box at 19px. 19 is the largest whole number under the 19.4px the row's
 *  own text line stands at, so the mark rides inside the row rather than setting its height -
 *  the list keeps its 33.4px - which is what the card head does with its two lines. */
export function Logo({ url, size = 40 }: { url: string; size?: number }) {
  const [broken, setBroken] = useState(false);
  return (
    <span className="logo" style={{ width: size, height: size }}>
      {url && !broken ? <img src={url} alt="" onError={() => setBroken(true)} /> : null}
    </span>
  );
}
