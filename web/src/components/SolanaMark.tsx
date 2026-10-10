/** The Solana chain's own mark (web3icons artwork, `networks/mono`, taken verbatim).
 *
 *  One filled path on the 24 grid: the three bands are the drawing itself, already skewed, so
 *  they cannot be redrawn as 1.5px lines without losing the mark. Same shape and the same
 *  source as ArcMark and RobinhoodMark, which is what lets the rail's chain rows read as one
 *  hand. `currentColor` rather than the artwork's own `#fff`, so it takes the ink of the word
 *  beside it like every other glyph in the rail instead of opening a colour of its own.
 *
 *  Provenance for the next one: `packages/core/src/svgs/networks/mono/solana.svg` from
 *  github.com/0xa3k5/web3icons.
 */
export function SolanaMark({ size = 14 }: { size?: number }) {
  return (
    <svg className="chain-mark" width={size} height={size} viewBox="0 0 24 24"
      fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M18.413 7.903a.62.62 0 0 1-.411.162H3.58c-.512 0-.77-.585-.416-.928l2.369-2.283a.6.6 0 0 1 .41-.17H20.42c.517 0 .77.591.41.935zm0 11.255a.62.62 0 0 1-.411.157H3.58c-.512 0-.77-.58-.416-.922l2.369-2.29a.6.6 0 0 1 .41-.163H20.42c.517 0 .77.585.41.928zm0-8.686a.62.62 0 0 0-.411-.157H3.58c-.512 0-.77.58-.416.922l2.369 2.29a.6.6 0 0 0 .41.163H20.42c.517 0 .77-.585.41-.928z" />
    </svg>
  );
}
