import type { Segment } from "../api/client";

/** Render stored report text.
 *
 * The API ships segments, never HTML, so there is no dangerouslySetInnerHTML
 * anywhere in this codebase and no way for stored text to inject markup.
 */
export function Rich({ segments }: { segments: Segment[] }) {
  return (
    <>
      {segments.map((segment, index) => {
        const key = `${index}-${segment.kind}`;
        if (segment.kind === "link" && segment.url) {
          return (
            <a key={key} href={segment.url} target="_blank" rel="noreferrer">
              {segment.text}
            </a>
          );
        }
        if (segment.kind === "bold") {
          return <b key={key}>{segment.text}</b>;
        }
        if (segment.kind === "code") {
          return <code key={key}>{segment.text}</code>;
        }
        return segment.text;
      })}
    </>
  );
}
