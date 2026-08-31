"use client";

import { DesktopAgent } from "../ui/DesktopAgent";
import { MobileAgent } from "../ui/MobileAgent";
import type { AgentWorkspaceProps } from "../ui/AgentWorkspace.types";

export function snapshotPollInterval(active: boolean): number {
  return active ? 2_000 : 8_000;
}

export function agentChannels(
  sessionId: string | null,
  generationIds: string[],
): string[] {
  if (!sessionId) return [];
  return [
    `agent:${sessionId}`,
    ...generationIds.map((id) => `task:${id}`),
  ];
}

export function busySessionId(input: {
  patching: boolean;
  patchSessionId?: string;
  deleting: boolean;
  deleteSessionId?: string;
}): string | null {
  if (input.patching) return input.patchSessionId ?? null;
  if (input.deleting) return input.deleteSessionId ?? null;
  return null;
}

export function removingImageId(
  pending: boolean,
  imageId: string | undefined,
): string | null {
  return pending ? imageId ?? null : null;
}

export function AgentWorkspaceView({
  platform,
  props,
}: {
  platform: "desktop" | "mobile";
  props: AgentWorkspaceProps;
}) {
  return platform === "mobile" ? (
    <MobileAgent {...props} />
  ) : (
    <DesktopAgent {...props} />
  );
}
