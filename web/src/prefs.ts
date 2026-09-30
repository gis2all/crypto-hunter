/** The page's own memory of where the reader is.
 *
 *  The theme keeps its choice here too (see `theme.ts`), and it is the one value that needs
 *  a pre-paint reader: a dark frame flashing before light is visible, and a stale tab is
 *  not. These three - which radar, which set of settings, which tab - are read once as a
 *  `useState` initial value, so a reload lands on the page that was open instead of back on
 *  Arc · 报告. Nothing here is a server fact: a browser that refuses storage just gets the
 *  defaults a first visit gets. */

export function readPref(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    // Private mode can block storage; a reader without it still gets the defaults.
    return null;
  }
}

export function savePref(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // A refused write is not a reason to refuse the placement for this session.
  }
}
