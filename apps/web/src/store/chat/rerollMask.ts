import type { Generation } from "../../lib/types";

export async function resolveRerollMask(
  generation: Generation,
  read: () => Promise<{ id: string; mask_image_id?: string | null }>,
): Promise<string | null> {
  if (generation.mask_image_id !== undefined) return generation.mask_image_id;
  if (generation.action !== "edit") return null;
  // An old/partial UI snapshot cannot prove that an edit had no mask.
  const source = await read();
  if (source.id !== generation.id ||
    !(source.mask_image_id === null ||
      (typeof source.mask_image_id === "string" && source.mask_image_id.length > 0))) {
    throw new Error("原任务遮罩信息不完整，未提交重跑，请刷新任务后重试");
  }
  return source.mask_image_id;
}
