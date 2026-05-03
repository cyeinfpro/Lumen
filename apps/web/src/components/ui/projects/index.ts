// projects 模块对外入口。
// page.tsx 直接 import { ProjectsIndex, ApparelWorkflowDetail, ApparelWorkflowNewPage }
// 即可，不再依赖以前的单文件 ApparelWorkflowShell.tsx。

export { ProjectsIndex } from "./ProjectsIndex";
export { ApparelWorkflowNewPage } from "./ApparelWorkflowNewPage";
export { ApparelWorkflowDetail } from "./ApparelWorkflowDetail";
