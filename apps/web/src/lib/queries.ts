
import { qk } from "./queries/queryKeys";

export { qk };
export * from "./queries/admin";
export * from "./queries/conversations";
export * from "./queries/projects";
export * from "./queries/poster";

export {
  useCreateSystemPromptMutation,
  useDeleteSystemPromptMutation,
  usePatchSystemPromptMutation,
  useSetDefaultSystemPromptMutation,
  useSystemPromptsQuery,
} from "./queries/systemPrompts";

// ——— Queries ———


// ——————————————————————————————————————————————————————————————
// 核心对话流：conversations / messages hooks（主页 + Sidebar 依赖）
// ——————————————————————————————————————————————————————————————


// ——————————————————————————————————————————————————————————————
// 结构化项目 / 工作流
// ——————————————————————————————————————————————————————————————



// ============================================================================
// Poster Style Library hooks（V1.1）
// ============================================================================
