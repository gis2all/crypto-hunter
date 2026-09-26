/** The Arc chain's own mark (web3icons artwork), drawn as it ships.
 *
 *  One filled path on the 24 grid, and the only filled glyph in the set: everything else here
 *  is a 1.5px stroke, which is what keeps a row of icons reading as one hand. This one is a
 *  silhouette whose counters are part of the drawing, so it cannot be redrawn as lines without
 *  losing the mark - hence the artwork verbatim, and `currentColor` so it takes the ink of the
 *  word beside it like every other glyph instead of opening a colour of its own.
 *
 *  A note for the next one: the first artwork that reached this slot was Robinhood's mark, not
 *  Arc's. If the Robinhood chain is ever wired up, that is its mark - it is not this path. */
export function ArcMark({ size = 14 }: { size?: number }) {
  return (
    <svg className="arc-mark" width={size} height={size} viewBox="0 0 24 24"
      fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M3.5 20.999c.146-4.407.893-8.519 2.142-11.717C7.223 5.231 9.513 3 12.088 3s4.865 2.231 6.447 6.283c.822 2.107 1.427 4.61 1.786 7.334q.048.366.087.737.015.024.013.041s.21 1.317.256 3.604h-.024c-.313-.256-4-3.153-10.112-2.314.093-1.035.22-2.04.383-3.005l.027-.146a24.5 24.5 0 0 1 6.104.57q-.007-.056-.017-.115c-.33-2.06-.819-3.945-1.448-5.556-1.029-2.635-2.371-4.271-3.502-4.271-1.132 0-2.474 1.636-3.503 4.271q-.375.958-.679 2.034a30 30 0 0 0-.718 3.213A40 40 0 0 0 6.662 21H3.5z" />
    </svg>
  );
}
