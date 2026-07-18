import { doesNotMatch, equal, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const browserSource = source("./PosterStyleBrowser.tsx");
const detailSource = source("./PosterStyleDetailDrawer.tsx");
const jobsPanelSource = source("./PosterStyleJobsPanel.tsx");
const pageSource = source("./PosterStylePage.tsx");

test("cover-only poster styles open the cover lightbox item", () => {
  match(detailSource, /if \(item\.samples\.length === 0\)/);
  match(detailSource, /id:\s*`\$\{item\.id\}#cover`/);
  match(
    detailSource,
    /previewUrl:\s*item\.display_url \?\? item\.cover_image_url/,
  );
  match(
    detailSource,
    /const initialId = activeSample\s*\?\s*`\$\{itemId\}#\$\{activeSample\.index\}`\s*:\s*`\$\{itemId\}#cover`/,
  );
  doesNotMatch(
    detailSource,
    /if \(lightboxItems\.length === 0 \|\| !activeSample\) return/,
  );
});

test("view-library requests are written before navigation, consumed, and cleared", () => {
  const writeIndex = pageSource.indexOf(
    'window.sessionStorage.setItem("posterStyle.openItemId", itemId)',
  );
  const tabIndex = pageSource.indexOf('setTab("browse")', writeIndex);
  const readIndex = browserSource.indexOf(
    '.getItem("posterStyle.openItemId")',
  );
  const clearIndex = browserSource.indexOf(
    'window.sessionStorage.removeItem("posterStyle.openItemId")',
  );
  const openIndex = browserSource.indexOf("setDetailItemId(pendingItemId)");

  ok(writeIndex >= 0, "missing one-shot item request write");
  ok(tabIndex > writeIndex, "browse navigation must follow the request write");
  ok(readIndex >= 0, "missing one-shot item request read");
  ok(clearIndex > readIndex, "one-shot request must be cleared after reading");
  ok(openIndex > clearIndex, "target detail must open after request cleanup");
});

test("poster style page keeps one jobs poller mounted across tabs", () => {
  const pageObserverIndex = pageSource.indexOf(
    "const jobs = usePosterStyleJobsQuery({ limit: 50 });",
  );
  const renderIndex = pageSource.indexOf("return (");
  const observerCount =
    [pageSource, jobsPanelSource]
      .join("\n")
      .match(/usePosterStyleJobsQuery\(/g)?.length ?? 0;

  ok(pageObserverIndex >= 0, "page must own the jobs observer");
  ok(
    pageObserverIndex < renderIndex,
    "jobs polling must be mounted independently of the selected tab",
  );
  equal(observerCount, 1, "page and jobs panel must not create duplicate pollers");
  match(
    pageSource,
    /<PosterStyleJobsPanel jobs=\{jobs\} onOpenItem=\{handleOpenItemFromJob\} \/>/,
  );
  match(jobsPanelSource, /jobs: PosterStyleJobsQueryResult/);
});
