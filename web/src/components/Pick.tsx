import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Icon } from "./Icon";

/** The list the app draws itself, wherever it offers a choice.
 *
 *  A native <select> hands its popup to the operating system: on this dark page that is a
 *  white menu with a blue bar in it, and no rule in the stylesheets can reach inside. So the
 *  list is drawn here instead, and the topbar’s config picker and the settings rows wear
 *  this same one rather than two that drift apart.
 *
 *  Two things a native <select> did for free are done here: a click anywhere else closes the
 *  list, and so does Escape - the same pair NoteHelp leans on, for the same reason. */
export function Pick({
  className,
  label,
  current,
  trigger,
  options,
  onPick,
}: {
  /** Classes for the trigger. Everywhere it is used it is `.btn.sec`, so the box matches the
   *  buttons it stands among. */
  className: string;
  /** aria-label of the list. */
  label: string;
  /** The value in force, so the list can mark it. */
  current: string;
  /** Trigger contents, before the chevron this control appends. */
  trigger: ReactNode;
  /** One entry per choice. `label` is a node, so a caller can put its own marks in a row. */
  options: { value: string; label: ReactNode }[];
  onPick: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const away = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const esc = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <span className="pick-wrap" ref={box}>
      <button
        type="button"
        className={className}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
      >
        {trigger}
        <Icon as={ChevronDown} size={14} />
      </button>
      {open ? (
        <div className="pick-menu" role="listbox" aria-label={label}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === current}
              className={`pick-item${option.value === current ? " on" : ""}`}
              onClick={() => {
                setOpen(false);
                onPick(option.value);
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </span>
  );
}