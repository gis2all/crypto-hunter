/** Dark or light, decided in one place.
 *
 *  The two themes are one token block each in `styles/tokens.css`; nothing else in the app
 *  knows they exist, because every colour already reads a custom property. Switching is
 *  therefore one attribute on <html> - no component re-renders and no prop beyond this one
 *  reaches the tree.
 *
 *  The choice is kept in localStorage and read again by an inline script in index.html
 *  before the first paint: waiting for this module would show a dark default for one frame
 *  on every reload, which is the flash the reader would notice most. */
export type Theme = "dark" | "light";

export const THEME_KEY = "ch.theme";

export function readTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    // Private mode can block storage; a reader without it still gets the default.
    return "dark";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  // The tab icon is its own document, so the attribute above cannot reach it: both
  // variants sit on the <link> as data attributes (index.html) and the theme picks one.
  // Same choice the pre-paint script makes, from the same place - one rule, two callers.
  const icon = document.getElementById("ch-icon");
  if (icon instanceof HTMLLinkElement) {
    const next = icon.dataset[theme];
    if (next) {
      icon.href = next;
    }
  }
}

export function saveTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // A refused write is not a reason to refuse the switch for this session.
  }
}
