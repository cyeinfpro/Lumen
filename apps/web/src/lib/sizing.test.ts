import assert from "node:assert/strict";
import test from "node:test";
import { PRESET, qualityToFixedSize } from "./sizing.ts";
import type { AspectRatio } from "./types";

for (const quality of ["1k", "2k", "4k"] as const) {
  for (const aspect of Object.keys(PRESET) as AspectRatio[]) {
    test(`${quality} ${aspect} preset satisfies explicit size constraints`, () => {
      const { fixed_size } = qualityToFixedSize(quality, aspect);
      assert.ok(fixed_size);
      const [width, height] = fixed_size.split("x").map(Number);
      assert.equal(width % 16, 0);
      assert.equal(height % 16, 0);
      assert.ok(Math.max(width, height) <= 3840);
      assert.ok(width * height >= 655360 && width * height <= 8294400);
      assert.ok(Math.max(width, height) * 9 <= Math.min(width, height) * 21);
      if (aspect === "21:9" || aspect === "9:21") {
        const [rw, rh] = aspect.split(":").map(Number);
        assert.equal(width * rh, height * rw);
      }
    });
  }
}
