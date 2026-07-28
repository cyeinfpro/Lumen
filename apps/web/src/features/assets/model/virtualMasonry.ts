export interface VirtualMasonryAsset {
  id: string;
  prompt: string;
  image: {
    width: number;
    height: number;
  };
}

export interface VirtualMasonryGroup<T extends VirtualMasonryAsset> {
  key: string;
  label: string;
  items: T[];
}

export interface VirtualMasonryTile<T extends VirtualMasonryAsset> {
  item: T;
  itemIndex: number;
  groupKey: string;
  column: number;
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface VirtualMasonryHeader {
  key: string;
  label: string;
  count: number;
  top: number;
}

export interface VirtualMasonryLayout<T extends VirtualMasonryAsset> {
  headers: VirtualMasonryHeader[];
  tiles: VirtualMasonryTile<T>[];
  totalHeight: number;
}

export const MOBILE_MOUNTED_TILE_BUDGET = 80;
export const DESKTOP_MOUNTED_TILE_BUDGET = 160;

const HEADER_HEIGHT = 42;
const GROUP_GAP = 20;
const METADATA_HEIGHT = 86;
const MIN_TILE_HEIGHT = 180;
const MAX_TILE_HEIGHT = 920;

function tileHeight<T extends VirtualMasonryAsset>(
  item: T,
  columnWidth: number,
): number {
  const width = Math.max(1, item.image.width || 1);
  const height = Math.max(1, item.image.height || 1);
  const mediaHeight = columnWidth * (height / width);
  const promptRows = Array.from(item.prompt).length > 34 ? 2 : 1;
  return Math.max(
    MIN_TILE_HEIGHT,
    Math.min(MAX_TILE_HEIGHT, mediaHeight + METADATA_HEIGHT + promptRows * 18),
  );
}

export function mountedTileBudget(columnCount: number): number {
  return columnCount <= 2
    ? MOBILE_MOUNTED_TILE_BUDGET
    : DESKTOP_MOUNTED_TILE_BUDGET;
}

export function layoutVirtualMasonry<T extends VirtualMasonryAsset>(
  groups: VirtualMasonryGroup<T>[],
  containerWidth: number,
  columnCount: number,
  gap: number,
): VirtualMasonryLayout<T> {
  const columns = Math.max(1, Math.floor(columnCount));
  const safeWidth = Math.max(1, containerWidth);
  const columnWidth = Math.max(
    1,
    (safeWidth - gap * (columns - 1)) / columns,
  );
  const headers: VirtualMasonryHeader[] = [];
  const tiles: VirtualMasonryTile<T>[] = [];
  let cursorTop = 0;
  let itemIndex = 0;

  for (const group of groups) {
    if (group.items.length === 0) continue;
    headers.push({
      key: group.key,
      label: group.label,
      count: group.items.length,
      top: cursorTop,
    });
    const columnTops = Array.from(
      { length: columns },
      () => cursorTop + HEADER_HEIGHT,
    );
    for (const item of group.items) {
      let column = 0;
      for (let index = 1; index < columns; index += 1) {
        if (columnTops[index] < columnTops[column]) column = index;
      }
      const height = tileHeight(item, columnWidth);
      tiles.push({
        item,
        itemIndex,
        groupKey: group.key,
        column,
        top: columnTops[column],
        left: column * (columnWidth + gap),
        width: columnWidth,
        height,
      });
      columnTops[column] += height + gap;
      itemIndex += 1;
    }
    cursorTop = Math.max(...columnTops) + GROUP_GAP;
  }

  return {
    headers,
    tiles,
    totalHeight: Math.max(1, cursorTop),
  };
}

function distanceToViewport<T extends VirtualMasonryAsset>(
  tile: VirtualMasonryTile<T>,
  viewportCenter: number,
): number {
  return Math.abs(tile.top + tile.height / 2 - viewportCenter);
}

export function selectVirtualMasonryTiles<T extends VirtualMasonryAsset>(
  tiles: VirtualMasonryTile<T>[],
  viewportStart: number,
  viewportEnd: number,
  overscan: number,
  maxMounted: number,
): VirtualMasonryTile<T>[] {
  const start = Math.max(0, viewportStart - Math.max(0, overscan));
  const end = Math.max(start, viewportEnd + Math.max(0, overscan));
  const visible = tiles.filter(
    (tile) => tile.top + tile.height >= start && tile.top <= end,
  );
  if (visible.length <= maxMounted) return visible;
  const center = (viewportStart + viewportEnd) / 2;
  return visible
    .slice()
    .sort(
      (a, b) =>
        distanceToViewport(a, center) - distanceToViewport(b, center),
    )
    .slice(0, maxMounted)
    .sort((a, b) => a.itemIndex - b.itemIndex);
}

export function virtualTileForId<T extends VirtualMasonryAsset>(
  tiles: VirtualMasonryTile<T>[],
  id: string,
): VirtualMasonryTile<T> | undefined {
  return tiles.find(
    (tile) =>
      tile.item.id === id ||
      ("id" in tile.item.image && tile.item.image.id === id),
  );
}
