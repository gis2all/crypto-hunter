import assert from "node:assert/strict";
import test from "node:test";

import { safeHttpUrl } from "../src/lib/urls.ts";

test("keeps ordinary web links", () => {
  assert.equal(safeHttpUrl("https://example.com/a"), "https://example.com/a");
  assert.equal(safeHttpUrl(" HTTP://Example.COM/path "), "http://example.com/path");
});

test("rejects clickable non-web schemes", () => {
  for (const raw of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "blob:https://example.com/id",
    "file:///C:/Windows/win.ini",
    "mailto:test@example.com",
  ]) {
    assert.equal(safeHttpUrl(raw), null);
  }
});

test("rejects credentials, relative URLs, and embedded whitespace", () => {
  for (const raw of [
    "https://user:pass@example.com/",
    "//example.com/path",
    "/relative",
    "https://example.com/a\nnext",
    "https://example.com/\\@127.0.0.1",
    "http://127.0.0.1:8787/",
    "http://localhost/",
    "http://intranet/",
    "http://198.18.0.218/",
    "https://example.com:8443/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
  ]) {
    assert.equal(safeHttpUrl(raw), null);
  }
});

test("keeps public IPv6 and default ports", () => {
  assert.equal(
    safeHttpUrl("https://[2606:4700:4700::1111]/"),
    "https://[2606:4700:4700::1111]/",
  );
  assert.equal(safeHttpUrl("https://example.com:443/"), "https://example.com/");
});
