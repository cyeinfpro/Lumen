export interface HistoryViewportRect {
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface HistoryNodeRect extends HistoryViewportRect { id: string; }

function finiteRect(rect: HistoryViewportRect): boolean {
  return [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)
    && rect.width > 0 && rect.height > 0;
}
function intersects(a: HistoryViewportRect, b: HistoryViewportRect): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x
    && a.y < b.y + b.height && a.y + a.height > b.y;
}

/** Recover only an empty view caused by moving existing nodes through history.
 * Config edits, historical branch insertion/removal and an intentionally empty
 * pre-action viewport must never move the user's camera.
 */
export function historyViewportRecoveryTargets(
  before: readonly HistoryNodeRect[],
  after: readonly HistoryNodeRect[],
  viewport: HistoryViewportRect,
): string[] {
  if (!finiteRect(viewport) || before.some((node) => !finiteRect(node))
    || after.some((node) => !finiteRect(node))) return [];
  const previous = new Map(before.map((node) => [node.id, node]));
  const moved = after.filter((node) => {
    const old = previous.get(node.id);
    return old && (node.x !== old.x || node.y !== old.y);
  });
  if (moved.length === 0 || !before.some((node) => intersects(node, viewport))
    || after.some((node) => intersects(node, viewport))) return [];
  return moved.map((node) => node.id);
}
