import { match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(
  new URL("./DesktopAccountMenu.tsx", import.meta.url),
  "utf8",
);

test("desktop account menu does not expose Docker-only admin routes", () => {
  match(source, /if \(isAdmin && !desktop\) \{/);
});
