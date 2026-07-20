import type { VideoProviderKind } from "@/lib/types";

import {
  VOLCANO_DEFAULT_REGION,
  draftWasRenamed,
  inferVolcanoRegion,
  issueTone,
  presetPatchForKind,
  type Draft,
  type Issue,
} from "./domain";
import type { StatusTone } from "./shared";

export type AssetCredentialSummary = {
  text: string;
  tone: StatusTone;
  label: string;
};

export function editorStatusLabel(
  tone: ReturnType<typeof issueTone>,
): string {
  if (tone === "success") return "可保存";
  if (tone === "danger") return "需修复";
  return "有提示";
}

export function providerKindPatch(
  draft: Draft,
  kind: VideoProviderKind,
): Partial<Draft> {
  return presetPatchForKind({ ...draft, kind });
}

export function baseUrlDraftPatch(
  draft: Draft,
  baseUrl: string,
): Partial<Draft> {
  const previousInferredRegion = inferVolcanoRegion(draft.base_url);
  const nextInferredRegion = inferVolcanoRegion(baseUrl);
  const followsBaseUrl =
    draft.kind === "volcano" &&
    Boolean(nextInferredRegion) &&
    (!draft.region.trim() ||
      draft.region === previousInferredRegion ||
      (!previousInferredRegion && draft.region === VOLCANO_DEFAULT_REGION));

  return {
    base_url: baseUrl,
    ...(followsBaseUrl && nextInferredRegion
      ? { region: nextInferredRegion }
      : {}),
  };
}

export function credentialPlaceholder(
  replacementRequired: boolean,
  storedHint: string,
): string {
  if (replacementRequired) return "重命名后需重填";
  if (storedHint) return `留空保留 ${storedHint}`;
  return "未配置";
}

export function assetCredentialSummary({
  replacementRequired,
  hasNew,
  hasCompleteNew,
  hasStored,
  storedReady,
  storedAccessKeyIdHint,
  storedSecretAccessKeyHint,
}: {
  replacementRequired: boolean;
  hasNew: boolean;
  hasCompleteNew: boolean;
  hasStored: boolean;
  storedReady: boolean;
  storedAccessKeyIdHint: string;
  storedSecretAccessKeyHint: string;
}): AssetCredentialSummary {
  if (hasNew && !hasCompleteNew) {
    return {
      text: "只填写了一项火山资产凭证，请同时填写 Access Key ID 与 Secret Access Key",
      tone: "danger",
      label: "凭证不完整",
    };
  }
  if (replacementRequired && !hasCompleteNew) {
    return {
      text: "供应商重命名后需重新填写 Access Key ID 与 Secret Access Key",
      tone: "danger",
      label: "保存前需重填",
    };
  }
  if (hasNew) {
    return {
      text: "将成对更新火山资产 Access Key ID 与 Secret Access Key",
      tone: storedReady ? "success" : "warning",
      label: "保存后校验",
    };
  }
  if (hasStored) {
    return {
      text: `留空将保留已保存凭证：${storedAccessKeyIdHint} / ${storedSecretAccessKeyHint}`,
      tone: storedReady ? "success" : "warning",
      label: storedReady ? "已保存配置可用" : "已保存配置未就绪",
    };
  }
  return {
    text: "尚未保存火山资产凭证",
    tone: "neutral",
    label: "未保存资产配置",
  };
}

export function providerKeyStatus(
  draft: Draft,
  storedKeyHint: string,
): string {
  if (draft.kind === "fake") return "测试供应商不需要 Key";
  if (draft.api_key.trim()) return "将更新为新 Key";
  if (draftWasRenamed(draft)) return "重命名后需重新填写 Key";
  if (storedKeyHint) return `保留已保存 Key：${storedKeyHint}`;
  return "未保存 Key";
}

export function formIssues(summaryIssues: Issue[] | undefined): Issue[] {
  return summaryIssues ?? [];
}
