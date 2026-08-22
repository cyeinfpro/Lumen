import assert from "node:assert/strict";
import test from "node:test";
import {
  getActiveNavKey,
  getAppNavItems,
  getFirstVisibleNavRoute,
  getFirstVisibleNavRouteExcluding,
  getRedirectForHiddenNavPath,
  normalizeNavVisibility,
} from "./navigation.ts";

test("Agent is ordered after Studio and remains fail-closed when missing", () => {
  assert.deepEqual(
    getAppNavItems({ agent: true }).map((item) => item.key),
    ["studio", "agent", "video", "projects", "assets", "me"],
  );
  assert.equal(normalizeNavVisibility({}).agent, false);
  assert.equal(getActiveNavKey("/agent", {}), null);
  assert.equal(getActiveNavKey("/agent", { agent: true }), "agent");
});

test("hidden navigation redirects to the first visible route and falls back to Me", () => {
  assert.equal(
    getRedirectForHiddenNavPath("/agent", {
      studio: false,
      agent: false,
      video: true,
    }),
    "/video",
  );
  const allHidden = {
    studio: false,
    agent: false,
    video: false,
    projects: false,
    assets: false,
  };
  assert.equal(getFirstVisibleNavRoute(allHidden), "/me");
  assert.equal(getRedirectForHiddenNavPath("/projects/one", allHidden), "/me");
  assert.equal(
    getFirstVisibleNavRouteExcluding("agent", {
      studio: false,
      agent: true,
      video: false,
      projects: false,
      assets: false,
    }),
    "/me",
  );
});
