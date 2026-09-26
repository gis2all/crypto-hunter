import { createLucideIcon } from "lucide-react";

/** The mark: a scope square, one sweep, one blip.
 *
 *  Chosen 2026-09-26 from a sheet of five directions (reticle / radar / funnel / bars /
 *  monogram). It is declared as a lucide icon rather than a bespoke <svg> so it can go
 *  through `Icon` like every other glyph on the screen: the `.ico` rule in app.css hands it
 *  the 1.5 stroke, the square caps and the mitre joins, `currentColor` hands it the theme,
 *  and it inherits the three approved sizes. A hand-written <svg> would repeat all four and
 *  could drift from the rest of the set.
 *
 *  It is not lucide's own `Radar`, which is concentric arcs - a round shape, and this
 *  screen has none. The square frame is what makes it read as a scope here.
 *
 *  The favicon in index.html is the same three landmarks, tuned for the one size a tab has.
 *  A 24-grid drawing at 16px puts every edge on a fraction of a pixel and a 1.5 stroke on
 *  1px, and the browser answers with a blur - the first cut of this mark was reported as
 *  `very blurry` on the tab. So the icon there uses a 3 stroke (exactly 2px at 16) and moves
 *  the sweep and the blip onto multiples of 1.5, which land on whole pixels at 16: sweep ends
 *  at 16.5/7.5 instead of 18/6, blip at 16.5/16.5 instead of 17/15. That also pulls both off
 *  the frame's inner edge, where they used to merge into it at tab size. Kept in step by hand,
 *  so a change to one is a change to the other. */
export const Mark = createLucideIcon({
  name: "ch-mark",
  size: 24,
  node: [
    ["path", { d: "M3 3h18v18H3z" }],
    ["path", { d: "m12 12 6-6" }],
    ["path", { d: "M17 15h.01" }],
  ],
});
