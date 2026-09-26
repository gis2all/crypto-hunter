import type { LucideIcon } from "lucide-react";

/** One icon, drawn in the geometry the rest of the screen is drawn in.
 *
 *  lucide-react draws for a 24-unit grid at a 2-unit stroke with round caps and round
 *  joins. Three of those four are declined, and all three in one rule in app.css (`.ico`)
 *  rather than as props here: `stroke-*` is inherited, and a stylesheet declaration beats
 *  the presentation attribute the set renders itself with - so a glyph added later cannot
 *  forget the geometry, and no call site has to repeat it.
 *
 *  Colour is not passed either. The set already draws with `currentColor`, so an icon takes
 *  the colour of the text it stands beside; that is what makes the whole set follow the
 *  theme with no new token and no second palette.
 *
 *  Three sizes, chosen by the text the icon stands beside: 12 for the 10.5px register
 *  (card hints, row labels, stage lines), 14 for the 12.5px one (buttons, links), and 16
 *  for the 15px one (the 参数设置 page title).
 *
 *  Every icon is `aria-hidden`, and that is not a detail of this component but the rule for
 *  the set: an icon here only ever repeats the word next to it, so the word is the label.
 *  A button with no word at all carries its own aria-label and title - see the 关闭 / 上一轮
 *  / 复制 CA controls - which is why none of them needs the icon to speak. */
export type IconSize = 12 | 14 | 16;

export function Icon({
  as: Glyph,
  size = 12,
  className,
}: {
  as: LucideIcon;
  size?: IconSize;
  className?: string;
}) {
  return (
    <Glyph
      className={className ? `ico ${className}` : "ico"}
      size={size}
      aria-hidden="true"
      focusable="false"
    />
  );
}
