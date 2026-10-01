/** The band a card wears under its link row, on the two pages that draw one: the shelf and
 *  the CA search page. A card cannot say these things about itself - which chain it is on,
 *  which set of settings it was read under, when it was read and when it was kept - because
 *  it is the same card on a report page where the round around it already says so.
 *
 *  Shared here rather than in either page: the two bands have to read alike, and a card with
 *  one foot in one place and a different one in the other is the same card twice.
 */

/** When it was, to the minute. The stored stamps carry seconds and an offset; the foot only
 *  ever needs the minute, and a locale-formatted date would differ per machine. */
export function stamp(iso: string | null | undefined): string {
  return (iso ?? "").slice(0, 16).replace("T", " ");
}

/** One line of the band, out of the parts that are actually known.
 *
 *  The bar is the one the card's own link row uses, so the band reads as the same kind of
 *  thing. A part that is missing is dropped rather than printed as a dash: a card from a
 *  search has no round and no set of settings, and a line of dashes says less than a short
 *  line does. */
export function parts(...values: (string | null | undefined)[]): string {
  return values.filter((value) => value).join(" ｜ ");
}

/** A field of that band: the label, and the value when there is one. A field whose value is
 *  missing is dropped whole, so a card kept by a search shows a shorter line rather than a
 *  bare label or a dash. */
export function field(label: string, value: string | null | undefined): string {
  return value ? label + ": " + value : "";
}

/** A chain id as a name: `base` -> `Base`, `bsc` -> `BSC`, `robin-hood` -> `Robin Hood`.
 *
 *  Ids stay lowercase - five primary keys carry them, they are file names, and a shell reads
 *  them - and that is a reason for the id, not for what a reader is shown. Three letters or
 *  fewer is the all-caps form people write (`bsc`, `sui`, `eth`); anything longer reads as a
 *  word.
 */
function chainIdName(chainId: string): string {
  return chainId
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((word) => (word.length <= 3 ? word.toUpperCase()
                                     : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ");
}

/** What a chain is called on the band. Read off every chain rather than off the rail's own
 *  rows, because a card can come from a chain this app does not screen - and that chain has
 *  no file here to be named by, so its id becomes a name rather than being printed as a slug
 *  (user, 2026-10-01). */
export function chainName(labels: Map<string, string>, chainId: string): string {
  return labels.get(chainId) ?? chainIdName(chainId);
}
