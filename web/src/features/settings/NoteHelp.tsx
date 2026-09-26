import { CircleHelp } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Icon } from "../../components/Icon";

/** A rule folded behind an icon, hover or click to open.
 *
 * The settings page carries two kinds of text. A reading of this round - the funnel
 * counts, the violated thresholds - has to be on the page. A rule about how a field
 * behaves is read once and then in the way on every later visit, so the API ships it as
 * `detail` and it opens from here instead of printing itself every time.
 *
 * Hover opens it; a click pins it open, because a touch screen has no hover. A click
 * anywhere else or Escape unpins. The popover is one div per line: `\n` is the only line
 * break the API promises, and rendering it as a `<br />` inside an inline span is how the
 * note blocks used to do it - a block per line keeps the markup honest and lets the lines
 * space themselves. */
export function NoteHelp({ text, label = "说明" }: { text: string; label?: string }) {
  const [hover, setHover] = useState(false);
  const [pinned, setPinned] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const open = hover || pinned;

  useEffect(() => {
    if (!pinned) return;
    const away = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setPinned(false);
    };
    const esc = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPinned(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [pinned]);

  if (!text) return null;

  return (
    <div
      className={`note-help${open ? " open" : ""}`}
      ref={box}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        type="button"
        className="note-help-btn"
        aria-expanded={open}
        onClick={() => setPinned((was) => !was)}
      >
        <Icon as={CircleHelp} />
        {label}
      </button>
      <div className="note-help-pop" role="note">
        {text.split("\n").map((line, index) => (
          <div className="note-help-line" key={index}>
            {line}
          </div>
        ))}
      </div>
    </div>
  );
}
