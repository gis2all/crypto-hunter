/** A score, the way a reader reads it: a whole number (2026-09-28, user).
 *
 * The API keeps the decimals, and it has to: the score bar is drawn from the real
 * proportion, so a rounded 55.4 would move the fill. Every number that reaches the page
 * goes through here instead, and out of one function they all round the same way -
 * half up, so 70.5 reads 71 rather than the 70 that round-half-even would print. */
export function whole(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(Math.round(value));
}
