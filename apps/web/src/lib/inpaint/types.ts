// 局部修改 (inpaint) 共享领域类型。
// 既被 MaskBoard 内部使用，也被 useInpaintStore 用于持久化草稿。
//
// 放在 lib 层，避免 store 与组件实现互相依赖。

export type Tool = "brush" | "eraser";

export interface Stroke {
  tool: Tool;
  /** 画笔半径（显示坐标系下的像素） */
  radius: number;
  /** 扁平 [x1, y1, x2, y2, ...] —— 显示坐标，导出时按 displayDims.scale 反投到原图分辨率 */
  points: number[];
}
