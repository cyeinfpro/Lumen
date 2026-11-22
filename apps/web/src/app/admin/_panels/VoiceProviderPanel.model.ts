export const BEIJING = "cn-beijing";
export const SINGAPORE = "ap-southeast-1";
export type VoiceRegion = typeof BEIJING | typeof SINGAPORE;
export type VoiceProviderStatus = {
  configured: boolean;
  ready: boolean;
  region: VoiceRegion;
  workspace_id: string | null;
};
export type VoiceProviderDraft = { region?: VoiceRegion; workspace?: string };
type VoiceProviderPatch = { api_key?: string; region?: VoiceRegion; workspace_id?: string };

export function voiceProviderPatch(
  status: VoiceProviderStatus,
  draft: VoiceProviderDraft,
  rawKey: string,
): { error: string } | { body: VoiceProviderPatch } {
  const workspace = (draft.workspace ?? status.workspace_id ?? "").trim().toLowerCase();
  const region = draft.region ?? status.region;
  const apiKey = rawKey.trim();
  if (workspace && !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(workspace)) {
    return { error: "Workspace ID 只能包含字母、数字和连字符，不能以连字符开头或结尾。" };
  }
  if (apiKey && !/^[!-~]{8,512}$/.test(apiKey)) {
    return { error: "请检查 Key 格式。Key 不能包含空格，长度须为 8–512 位。" };
  }
  const oldWorkspace = status.workspace_id ?? "";
  const scopeChanged = region !== status.region || workspace !== oldWorkspace;
  if (!apiKey && (!status.configured || scopeChanged)) {
    return { error: status.configured ? "更换地区或 Workspace 时，请填入对应的新 Key。" : "请输入阿里云百炼 API Key。" };
  }
  const body: VoiceProviderPatch = {};
  if (apiKey) body.api_key = apiKey;
  if (region !== status.region) body.region = region;
  if (workspace !== oldWorkspace) body.workspace_id = workspace;
  return { body };
}

export function voiceProviderStatusLabel(status: VoiceProviderStatus) {
  if (!status.configured) return "未配置";
  return status.ready ? "已启用" : "已保存 · 等待服务就绪";
}
