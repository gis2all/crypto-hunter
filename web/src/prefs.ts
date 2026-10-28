/** The page's own memory of where the reader is.
 *
 *  The theme keeps its choice here too (see `theme.ts`), and it is the one value that needs
 *  a pre-paint reader: a dark frame flashing before light is visible, and a stale tab is
 *  not. These - which radar, which place, the set of settings that radar was reading and the
 *  tab it was on (one pair per radar), the 报告 / 设定 side of 新机会 (`ch.opportunity.view`),
 *  the id of the CA search this page queued, and which searched card it was showing - are read once as a `useState` initial value, so a reload
 *  lands on the page that was open instead of back on Arc · 报告. The pair is one key per
 *  chain (`ch.config.<id>` / `ch.view.<id>`), because a config id belongs to its own chain and a
 *  set read on one chain reads as deleted on the other; `App.tsx` writes the outgoing pair
 *  before it reads the incoming one. Nothing here is a server fact: a browser that refuses
 *  storage just gets the defaults a first visit gets. */

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
