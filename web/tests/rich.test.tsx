import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Rich } from "../src/components/Rich";

test("Rich renders unsafe link segments as text, not anchors", () => {
  const html = renderToStaticMarkup(
    <Rich
      segments={[
        { kind: "link", text: "click", url: "javascript:alert(1)" },
        { kind: "link", text: "local", url: "http://127.0.0.1:8787/" },
      ]}
    />,
  );
  assert.equal(html, "clicklocal");
});

test("Rich keeps safe links and hardens the new-tab boundary", () => {
  const html = renderToStaticMarkup(
    <Rich segments={[{ kind: "link", text: "site", url: "https://example.com/" }]} />,
  );
  assert.match(html, /href="https:\/\/example\.com\/"/);
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer nofollow"/);
  assert.match(html, /referrerPolicy="no-referrer"/);
});
