// 历史入口兜底：原本所有 stage / 控制台都塞在这一文件。
// 重构后拆分到 ./components 与 ./stages，并通过 ./index.ts 导出。
// 这里仅保留 re-export，避免外部仍按旧路径 import 时编译失败。

export {
  ApparelWorkflowDetail,
  ApparelWorkflowNewPage,
  ProjectsIndex,
} from "./index";
