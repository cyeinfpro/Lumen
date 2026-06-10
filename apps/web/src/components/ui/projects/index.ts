// projects 模块对外入口。
// page.tsx 直接 import { ProjectFunctionHub, ProjectsIndex, ApparelWorkflowDetail, ApparelWorkflowNewPage }
// 即可，不再依赖以前的单文件 ApparelWorkflowShell.tsx。

export { ProjectFunctionHub } from "./ProjectFunctionHub";
export { ProjectsIndex } from "./ProjectsIndex";
export { ApparelWorkflowNewPage } from "./ApparelWorkflowNewPage";
export { ApparelWorkflowDetail } from "./ApparelWorkflowDetail";
export { PosterWorkflowNewPage } from "./PosterWorkflowNewPage";
export { PosterWorkflowDetail } from "./PosterWorkflowDetail";
export { StoryboardDetailPage, StoryboardIndexPage } from "./storyboard";
