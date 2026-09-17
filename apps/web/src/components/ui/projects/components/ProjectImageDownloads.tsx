"use client";

import { useEffect, useRef, useState } from "react";
import { Download } from "lucide-react";
import { useUserQueryScope } from "@/components/QueryProvider";
import { Button } from "@/components/ui/primitives/Button";
import { downloadImageFile, type ImageDownloadFile } from "@/lib/imageDownload";

interface DownloadState {
  scope: string;
  busy: boolean;
  total: number;
  dispatched: number;
  processed: number;
  remaining: ImageDownloadFile[];
  error: string | null;
  stopped: boolean;
}

export function useProjectImageDownloads(scope: string) {
  const user = useUserQueryScope();
  const identity = `${user.userId}:${scope}`;
  const [state, setState] = useState<DownloadState | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => {
    active.current?.abort();
    active.current = null;
  }, [identity]);

  const start = async (files: ImageDownloadFile[]) => {
    if (active.current || !files.length) return;
    const controller = new AbortController();
    active.current = controller;
    const next: DownloadState = {
      scope: identity, busy: true, total: files.length, dispatched: 0,
      processed: 0, remaining: [], error: null, stopped: false,
    };
    setState({ ...next });
    for (let index = 0; index < files.length; index += 1) {
      if (controller.signal.aborted) {
        next.remaining.push(...files.slice(index));
        break;
      }
      try {
        await downloadImageFile(files[index], controller.signal);
        next.dispatched += 1;
      } catch (error) {
        next.remaining.push(files[index]);
        if (!controller.signal.aborted) {
          next.error = error instanceof Error ? error.message : "下载失败，请重试";
        }
      }
      next.processed += 1;
      if (active.current === controller) setState({ ...next, remaining: [...next.remaining] });
    }
    if (active.current !== controller) return;
    active.current = null;
    setState({ ...next, busy: false, stopped: controller.signal.aborted });
  };

  const current = state?.scope === identity ? state : null;
  return {
    state: current,
    busy: current?.busy ?? false,
    start,
    cancel: () => active.current?.abort(),
    retry: () => { if (current) void start(current.remaining); },
  };
}

type Downloads = ReturnType<typeof useProjectImageDownloads>;

export function ProjectImageDownloadStatus({ downloads }: { downloads: Downloads }) {
  const state = downloads.state;
  if (!state) return null;
  return (
    <div className="grid gap-2 py-2">
      <p role="status" aria-atomic="true" className="type-caption text-[var(--fg-1)]">
        {state.busy
          ? `准备下载 ${Math.min(state.processed + 1, state.total)} / ${state.total}`
          : `${state.stopped ? "已停止。" : ""}已发起 ${state.dispatched} 张下载，未完成 ${state.remaining.length} 张。`}
      </p>
      {state.error && <p role="alert" className="type-caption text-danger">{state.error}</p>}
      {state.busy ? (
        <Button variant="secondary" size="sm" onClick={downloads.cancel}>停止下载</Button>
      ) : state.remaining.length > 0 ? (
        <Button variant="secondary" size="sm" onClick={downloads.retry}>重试未完成的下载</Button>
      ) : null}
    </div>
  );
}

export function ProjectImageDownloadButton({ file, disabled = false, label = "下载图片" }: {
  file: ImageDownloadFile;
  disabled?: boolean;
  label?: string;
}) {
  const downloads = useProjectImageDownloads(file.url);
  return (
    <div>
      <Button variant="outline" size="sm" fullWidth
        aria-label={label}
        disabled={disabled || !file.url}
        loading={downloads.busy}
        onClick={() => void downloads.start([file])}
        leftIcon={<Download className="h-3.5 w-3.5" />}>
        {downloads.busy ? "准备下载…" : "下载"}
      </Button>
      {!file.url && <p className="mt-1 type-caption text-[var(--fg-2)]">图片暂不可下载</p>}
      <ProjectImageDownloadStatus downloads={downloads} />
    </div>
  );
}
