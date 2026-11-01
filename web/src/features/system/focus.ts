/** Which block the rail's status row asked for.

The rail and the settings page are mounted by the same shell, so one module-level value and one
event are enough. The request is only consumed once the block is actually in the DOM: navigating
from another place mounts the page before its query answers (2026-10-31). */

let pending = "";

export function askSystemBlock(id: string) {
  pending = id;
  window.dispatchEvent(new Event("ch:system-focus"));
}

export function systemBlock(): string {
  return pending;
}

export function clearSystemBlock() {
  pending = "";
}
