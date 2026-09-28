import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { Icon } from "./Icon";

/** The CA is the one value a reader takes away from a card or an archive row - to an
 *  explorer, a wallet, a chat - so it copies in one click and says so by turning green for a
 *  moment. There is no toast in this layout, and a second announcement of the same act would
 *  be noise.
 *
 *  It used to say 已复制 in words, and that word is wider than the glyph it replaced: the
 *  button grew on the click and pushed the row it sits in, in the one cell where width is
 *  priced - a 42-character address in a fixed column. The tick says the same thing at the
 *  size the icon set draws, and the button is one width in both states; `title` and
 *  `aria-label` carry the word for a reader who needs it spoken.
 *
 *  One button for both places on purpose: the overview table and the report card say the same
 *  thing, so they should look the same and fail the same way. The two sizes it is drawn at
 *  (12 in the table, 14 beside the card's CA box) are a rule in app.css rather than a prop,
 *  because it is the same button either way. */
export function CopyCa({ ca }: { ca: string }) {
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!done) return;
    const timer = window.setTimeout(() => setDone(false), 1400);
    return () => window.clearTimeout(timer);
  }, [done]);

  return (
    <button
      type="button"
      className={"ca-copy" + (done ? " done" : "")}
      aria-label={done ? "已复制" : "复制 CA"}
      title={done ? "已复制" : "复制 CA"}
      onClick={() => {
        void navigator.clipboard?.writeText(ca);
        setDone(true);
      }}
    >
      <Icon as={done ? Check : Copy} />
    </button>
  );
}
