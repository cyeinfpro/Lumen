"use client";

import { use } from "react";

import { StoryboardDetailPage } from "@/components/ui/projects";

export default function Page({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = use(params);
  return <StoryboardDetailPage storyboardId={runId} />;
}

