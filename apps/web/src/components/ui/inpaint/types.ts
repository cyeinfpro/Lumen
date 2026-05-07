// 局部修改 (inpaint) 共享类型。
// 既被 MaskBoard 内部使用，也被 useInpaintStore 用于持久化草稿。
//
// 不放在 useInpaintStore 是为了避免组件 import store-internal type 的隐式耦合：
// store 是更"基础"的层，类型从这里向上提供。

export type Tool = "brush" | "eraser";

export interface Stroke {
  tool: Tool;
  /** 画笔半径（显示坐标系下的像素） */
  radius: number;
  /** 扁平 [x1, y1, x2, y2, ...] —— 显示坐标，导出时按 displayDims.scale 反投到原图分辨率 */
  points: number[];
}
