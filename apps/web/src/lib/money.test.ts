import { equal } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../test-support/load-ts-module.mjs";

const { formatRmb, formatRmbCompact } = loadTsModule(
  new URL("./money.ts", import.meta.url),
) as {
  formatRmb(value?: string | number | null, fractionDigits?: number): string;
  formatRmbCompact(value?: string | number | null): string;
};

test("money formatters distinguish blank values from a real zero", () => {
  for (const value of ["", " ", "\n\t"]) {
    equal(formatRmb(value), "--");
    equal(formatRmbCompact(value), "--");
  }
  equal(formatRmb("0"), "0.00");
  equal(formatRmbCompact(0), "0.00");
});
