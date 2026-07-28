import { equal, ok } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const {
  DESKTOP_MOUNTED_TILE_BUDGET,
  MOBILE_MOUNTED_TILE_BUDGET,
  layoutVirtualMasonry,
  mountedTileBudget,
  selectVirtualMasonryTiles,
} = loadTsModule(new URL("./VirtualMasonry.ts", import.meta.url)) as {
  DESKTOP_MOUNTED_TILE_BUDGET: number;
  MOBILE_MOUNTED_TILE_BUDGET: number;
  layoutVirtualMasonry(
    groups: Array<{
      key: string;
      label: string;
      items: Array<{
        id: string;
        prompt: string;
        image: { width: number; height: number };
      }>;
    }>,
    width: number,
    columns: number,
    gap: number,
  ): {
    totalHeight: number;
    tiles: Array<{ itemIndex: number; top: number; height: number }>;
  };
  mountedTileBudget(columns: number): number;
  selectVirtualMasonryTiles(
    tiles: Array<{ itemIndex: number; top: number; height: number }>,
    start: number,
    end: number,
    overscan: number,
    max: number,
  ): Array<{ itemIndex: number; top: number; height: number }>;
};

function fixture(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    id: `generation-${index}`,
    prompt: `fixture ${index}`,
    image: {
      width: index % 3 === 0 ? 1024 : 1536,
      height: index % 4 === 0 ? 1536 : 1024,
    },
  }));
}

test("1000 assets keep desktop and mobile mounted sets within budgets", () => {
  for (const columns of [2, 4]) {
    const layout = layoutVirtualMasonry(
      [{ key: "all", label: "全部", items: fixture(1000) }],
      columns === 2 ? 640 : 1280,
      columns,
      columns === 2 ? 8 : 14,
    );
    const budget = mountedTileBudget(columns);
    const mounted = selectVirtualMasonryTiles(
      layout.tiles,
      layout.totalHeight / 2,
      layout.totalHeight / 2 + 900,
      1800,
      budget,
    );
    ok(mounted.length > 0);
    ok(mounted.length <= budget);
  }
  equal(mountedTileBudget(2), MOBILE_MOUNTED_TILE_BUDGET);
  equal(mountedTileBudget(3), DESKTOP_MOUNTED_TILE_BUDGET);
});

test("virtual layout height grows while mounted count remains independent", () => {
  const small = layoutVirtualMasonry(
    [{ key: "all", label: "全部", items: fixture(100) }],
    1280,
    4,
    14,
  );
  const large = layoutVirtualMasonry(
    [{ key: "all", label: "全部", items: fixture(1000) }],
    1280,
    4,
    14,
  );
  ok(large.totalHeight > small.totalHeight);
  const top = selectVirtualMasonryTiles(
    large.tiles,
    0,
    900,
    1350,
    DESKTOP_MOUNTED_TILE_BUDGET,
  );
  ok(top.length <= DESKTOP_MOUNTED_TILE_BUDGET);
});
