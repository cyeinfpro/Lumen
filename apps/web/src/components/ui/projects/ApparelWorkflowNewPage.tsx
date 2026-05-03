"use client";

// 创建新项目页：
// 1) 真拖拽（dragenter/dragleave/dragover/drop）+ 高亮放置区
// 2) 单文件级别进度条（XHR.upload.onprogress）+ 失败重试 + 取消
// 3) 并发上传（Promise.allSettled，失败的可重试再上传）
// 4) 文件大小 / 类型 / 数量校验
// 5) 字符计数（标题 / 基础需求）

import { ArrowLeft, Loader2, Trash2, Upload, WandSparkles, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { useCreateApparelWorkflowMutation } from "@/lib/queries";
import { API_BASE } from "@/lib/apiClient";
import { readCookie } from "@/lib/api/http";
import { cn } from "@/lib/utils";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectTopBar } from "./components/ProjectTopBar";
import { InfoPanel } from "./components/StageFrame";
import { MAX_PRODUCT_IMAGES, MAX_PRODUCT_IMAGE_BYTES } from "./types";
import { formatBytes } from "./utils";

const TITLE_MAX = 60;
const PROMPT_MAX = 240;
const ACCEPT = ["image/png", "image/jpeg", "image/webp"];

const AGE_SEGMENTS = [
  ["不指定", ""],
  ["幼儿", "幼儿"],
  ["儿童", "儿童"],
  ["青少年", "青少年"],
  ["青年", "青年"],
  ["成年", "成年"],
  ["中老年", "中老年"],
  ["老年", "老年"],
] as const;

const GENDERS = [
  ["女", "女性"],
  ["男", "男性"],
] as const;

const APPEARANCE_DIRECTIONS = [
  ["不限", ""],
  ["欧美", "欧美"],
  ["亚洲", "亚洲"],
  ["拉美", "拉美"],
  ["中东", "中东"],
  ["非洲", "非洲"],
] as const;

const STYLE_DIRECTIONS = [
  ["自然日常", "自然日常"],
  ["运动活力", "运动活力"],
  ["高级简洁", "高级简洁"],
  ["甜美亲和", "甜美亲和"],
  ["酷感街头", "酷感街头"],
  ["商务通勤", "商务通勤"],
] as const;

interface PendingFile {
  uid: string;
  file: File;
  url: string;
  progress: number;
  status: "queued" | "uploading" | "done" | "error" | "canceled";
  error?: string;
  uploadedId?: string;
  controller?: AbortController;
  xhr?: XMLHttpRequest;
}

function uid() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

interface UploadResult {
  id: string;
  width: number;
  height: number;
  url: string;
  mime?: string;
}

// XHR 上传：支持进度 + abort。CSRF token 走 cookie。
async function uploadWithProgress(
  file: File,
  onProgress: (ratio: number) => void,
  signal: AbortSignal,
): Promise<UploadResult> {
  // 同 apiFetch.refreshCsrfToken 逻辑：先读 cookie 兜底
  let csrf = readCookie("csrf");
  if (!csrf) {
    try {
      const res = await fetch(`${API_BASE.replace(/\/$/, "")}/auth/csrf`, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
      });
      if (res.ok) {
        const data = (await res.json().catch(() => null)) as
          | { csrf_token?: unknown }
          | null;
        if (typeof data?.csrf_token === "string") csrf = data.csrf_token;
      }
    } catch {
      // ignore；让请求自身报错出来
    }
    if (!csrf) csrf = readCookie("csrf");
  }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE.replace(/\/$/, "")}/images/upload`);
    xhr.withCredentials = true;
    if (csrf) xhr.setRequestHeader("x-csrf-token", csrf);

    const fd = new FormData();
    fd.append("file", file);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText) as UploadResult;
          resolve(data);
        } catch {
          reject(new Error("响应解析失败"));
        }
      } else if (xhr.status === 401) {
        reject(new Error("未登录或会话已失效"));
      } else if (xhr.status === 413) {
        reject(new Error("图片体积超过服务器限制"));
      } else {
        reject(new Error(`上传失败：HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("网络错误，请检查连接"));
    xhr.onabort = () => reject(new DOMException("已取消", "AbortError"));

    if (signal.aborted) {
      xhr.abort();
      reject(new DOMException("已取消", "AbortError"));
      return;
    }
    signal.addEventListener("abort", () => xhr.abort());

    xhr.send(fd);
  });
}

export function ApparelWorkflowNewPage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<PendingFile[]>([]);
  const [projectTitle, setProjectTitle] = useState("服饰模特展示图");
  const [ageSegment, setAgeSegment] = useState("成年");
  const [gender, setGender] = useState("女性");
  const [appearanceDirection, setAppearanceDirection] = useState("");
  const [styleDirection, setStyleDirection] = useState("高级简洁");
  const [extraPrompt, setExtraPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [dragActive, setDragActive] = useState(false);

  const createMutation = useCreateApparelWorkflowMutation({
    onError: (err) =>
      toast.error("创建项目失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: (out) => {
      toast.success("项目已创建");
      router.push(`/projects/${out.workflow_run_id}`);
    },
  });

  const titleRemaining = TITLE_MAX - projectTitle.length;
  const composedPrompt = useMemo(() => {
    const parts = [
      ageSegment ? `年龄段：${ageSegment}` : "",
      gender ? `性别：${gender}` : "",
      appearanceDirection ? `外貌方向：${appearanceDirection}` : "",
      styleDirection ? `风格气质：${styleDirection}` : "",
      extraPrompt.trim() ? `补充说明：${extraPrompt.trim()}` : "",
    ].filter(Boolean);
    return parts.join("，") || "自然电商服饰模特展示";
  }, [ageSegment, gender, appearanceDirection, styleDirection, extraPrompt]);
  const promptRemaining = PROMPT_MAX - composedPrompt.length;

  // 释放 ObjectURL，避免内存泄漏
  useEffect(() => {
    return () => {
      files.forEach((item) => URL.revokeObjectURL(item.url));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const validateFile = (file: File): string | null => {
    if (!ACCEPT.includes(file.type)) return `不支持的格式：${file.type || "未知"}`;
    if (file.size > MAX_PRODUCT_IMAGE_BYTES)
      return `单张不能超过 ${formatBytes(MAX_PRODUCT_IMAGE_BYTES)}`;
    return null;
  };

  const addFiles = (incoming: File[]) => {
    setError(null);
    const slots = MAX_PRODUCT_IMAGES - files.length;
    if (slots <= 0) {
      toast.warning(`最多 ${MAX_PRODUCT_IMAGES} 张`);
      return;
    }
    const next: PendingFile[] = [];
    for (const file of incoming.slice(0, slots)) {
      const reason = validateFile(file);
      if (reason) {
        toast.error(`${file.name}：${reason}`);
        continue;
      }
      next.push({
        uid: uid(),
        file,
        url: URL.createObjectURL(file),
        progress: 0,
        status: "queued",
      });
    }
    if (next.length) setFiles((prev) => [...prev, ...next]);
    if (incoming.length > slots) {
      toast.warning(`最多 ${MAX_PRODUCT_IMAGES} 张，超出 ${incoming.length - slots} 张已忽略`);
    }
  };

  const onPickFiles = (list: FileList | null) => {
    if (!list) return;
    addFiles(Array.from(list));
  };

  const removeFile = (uidToRemove: string) => {
    setFiles((prev) => {
      const target = prev.find((item) => item.uid === uidToRemove);
      if (target) {
        target.controller?.abort();
        URL.revokeObjectURL(target.url);
      }
      return prev.filter((item) => item.uid !== uidToRemove);
    });
  };

  const moveFile = (uidToMove: string, direction: -1 | 1) => {
    setFiles((prev) => {
      const idx = prev.findIndex((item) => item.uid === uidToMove);
      if (idx < 0) return prev;
      const next = idx + direction;
      if (next < 0 || next >= prev.length) return prev;
      const copy = [...prev];
      [copy[idx], copy[next]] = [copy[next], copy[idx]];
      return copy;
    });
  };

  const onDrop = (event: React.DragEvent) => {
    event.preventDefault();
    setDragActive(false);
    const list = Array.from(event.dataTransfer.files);
    addFiles(list);
  };

  const uploadOne = useCallback(async (target: PendingFile): Promise<string | null> => {
    const controller = new AbortController();
    setFiles((prev) =>
      prev.map((item) =>
        item.uid === target.uid
          ? { ...item, status: "uploading", progress: 0, error: undefined, controller }
          : item,
      ),
    );
    try {
      const result = await uploadWithProgress(
        target.file,
        (ratio) =>
          setFiles((prev) =>
            prev.map((item) =>
              item.uid === target.uid ? { ...item, progress: ratio } : item,
            ),
          ),
        controller.signal,
      );
      setFiles((prev) =>
        prev.map((item) =>
          item.uid === target.uid
            ? { ...item, status: "done", progress: 1, uploadedId: result.id }
            : item,
        ),
      );
      return result.id;
    } catch (err) {
      const message =
        err instanceof DOMException && err.name === "AbortError"
          ? "已取消"
          : err instanceof Error
            ? err.message
            : "上传失败";
      setFiles((prev) =>
        prev.map((item) =>
          item.uid === target.uid
            ? {
                ...item,
                status: err instanceof DOMException && err.name === "AbortError"
                  ? "canceled"
                  : "error",
                error: message,
              }
            : item,
        ),
      );
      return null;
    }
  }, []);

  const onCreate = async () => {
    setError(null);
    if (!files.length) {
      setError(`请上传 1 到 ${MAX_PRODUCT_IMAGES} 张商品图`);
      return;
    }
    if (composedPrompt.length > PROMPT_MAX) {
      setError("基础参数过长，请精简补充说明");
      return;
    }
    setSubmitting(true);
    try {
      // 把所有未完成的并发上传起来，取每个文件的最终 id
      const results = await Promise.all(
        files.map(async (file) =>
          file.status === "done" && file.uploadedId
            ? file.uploadedId
            : await uploadOne(file),
        ),
      );
      const ids = results.filter((id): id is string => Boolean(id));
      if (ids.length !== files.length) {
        toast.warning("部分图片未能上传，可重试单张或移除后重新创建");
        setSubmitting(false);
        return;
      }
      createMutation.mutate({
        product_image_ids: ids,
        user_prompt: composedPrompt,
        quality_mode: "premium",
        title: projectTitle.trim() || "服饰模特展示图",
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建项目失败");
    } finally {
      setSubmitting(false);
    }
  };

  const allDone = files.length > 0 && files.every((file) => file.status === "done");
  const anyUploading = files.some((file) => file.status === "uploading");
  const totalProgress = useMemo(() => {
    if (!files.length) return 0;
    return files.reduce((acc, file) => acc + file.progress, 0) / files.length;
  }, [files]);

  return (
    <div className="flex min-h-[100dvh] flex-col bg-[var(--bg-0)]">
      <OnlineBanner />
      <ProjectTopBar
        leadingSlot={
          <Link
            href="/projects"
            className="inline-flex items-center gap-1 truncate text-sm text-[var(--fg-1)] hover:text-[var(--fg-0)]"
          >
            项目 / 新建
          </Link>
        }
      />

      <main className="flex-1 overflow-y-auto px-4 py-5 md:px-8">
        <div className="mx-auto grid max-w-[1120px] gap-5 lg:grid-cols-[1fr_320px]">
          <section className="space-y-5">
            <div>
              <Link
                href="/projects"
                className="inline-flex items-center gap-1.5 text-sm text-[var(--fg-2)] hover:text-[var(--fg-0)]"
              >
                <ArrowLeft className="h-4 w-4" />
                返回项目列表
              </Link>
              <h1 className="mt-3 text-[26px] font-semibold tracking-normal md:text-[32px]">
                新建服饰模特展示图
              </h1>
              <p className="mt-1 text-sm text-[var(--fg-2)]">
                上传 1-3 张商品图，先确认 AI 合成的模特，再一次性生成 4 张电商展示图。
              </p>
            </div>

            <div className="rounded-md border border-[var(--border)] bg-white/[0.035] p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 className="text-sm font-medium text-[var(--fg-0)]">
                  商品图
                  <span className="ml-2 text-[11px] text-[var(--fg-2)]">
                    支持 PNG / JPEG / WebP，单张 ≤ {formatBytes(MAX_PRODUCT_IMAGE_BYTES)}
                  </span>
                </h2>
                <span className="text-xs tabular-nums text-[var(--fg-2)]">
                  {files.length}/{MAX_PRODUCT_IMAGES}
                </span>
              </div>

              <div
                onDragEnter={(event) => {
                  event.preventDefault();
                  setDragActive(true);
                }}
                onDragOver={(event) => {
                  event.preventDefault();
                  if (!dragActive) setDragActive(true);
                }}
                onDragLeave={(event) => {
                  // 只有真正离开容器时才取消高亮
                  if (event.currentTarget.contains(event.relatedTarget as Node)) return;
                  setDragActive(false);
                }}
                onDrop={onDrop}
                className={cn(
                  "relative",
                )}
              >
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className={cn(
                    "flex min-h-44 w-full flex-col items-center justify-center gap-3 rounded-md border border-dashed text-center transition-all duration-[var(--dur-base)]",
                    dragActive
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] shadow-[var(--shadow-amber)]"
                      : "border-[var(--border-strong)] bg-[var(--bg-1)] hover:bg-white/[0.04]",
                  )}
                >
                  <span
                    className={cn(
                      "inline-flex h-10 w-10 items-center justify-center rounded-full transition-colors",
                      dragActive
                        ? "bg-[var(--accent)] text-black"
                        : "bg-white/[0.06] text-[var(--fg-2)]",
                    )}
                  >
                    <Upload className="h-5 w-5" />
                  </span>
                  <span className="text-sm text-[var(--fg-1)]">
                    {dragActive
                      ? "松开即可加入项目"
                      : "拖拽图片到这里，或点击选择"}
                  </span>
                  <span className="text-[11px] text-[var(--fg-2)]">
                    最多 {MAX_PRODUCT_IMAGES} 张 · 第一张作为主图
                  </span>
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={ACCEPT.join(",")}
                  multiple
                  className="hidden"
                  onChange={(event) => {
                    onPickFiles(event.target.files);
                    event.target.value = "";
                  }}
                />
              </div>

              {files.length > 0 ? (
                <ul className="mt-3 grid gap-2 sm:grid-cols-3">
                  {files.map((item, index) => (
                    <li
                      key={item.uid}
                      className={cn(
                        "group relative overflow-hidden rounded-md border bg-[var(--bg-2)]",
                        item.status === "error"
                          ? "border-[var(--danger)]/40"
                          : item.status === "done"
                            ? "border-[var(--success)]/30"
                            : "border-[var(--border)]",
                      )}
                    >
                      <div className="relative aspect-[4/5]">
                        <img
                          src={item.url}
                          alt={item.file.name}
                          className="h-full w-full object-cover"
                        />
                        {item.status === "uploading" ? (
                          <div className="absolute inset-0 flex items-center justify-center bg-black/45 text-xs text-white">
                            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                            {Math.round(item.progress * 100)}%
                          </div>
                        ) : null}
                        {item.status === "error" ? (
                          <div className="absolute inset-x-0 bottom-0 bg-[var(--danger)]/90 px-1.5 py-1 text-[10px] text-white line-clamp-2">
                            {item.error}
                          </div>
                        ) : null}
                        {index === 0 ? (
                          <span className="absolute left-1.5 top-1.5 rounded-full bg-[var(--accent)] px-1.5 py-0.5 text-[10px] font-medium text-black">
                            主图
                          </span>
                        ) : null}
                      </div>
                      <div className="flex items-center justify-between gap-1 px-2 py-1 text-[10px] text-[var(--fg-2)]">
                        <span className="truncate" title={item.file.name}>
                          {formatBytes(item.file.size)}
                        </span>
                        <div className="flex items-center gap-0.5">
                          {item.status === "error" ? (
                            <button
                              type="button"
                              aria-label="重新上传"
                              onClick={() => uploadOne(item)}
                              className="rounded-md px-1.5 py-0.5 text-[var(--amber-300)] transition-colors hover:bg-white/[0.06]"
                            >
                              重试
                            </button>
                          ) : null}
                          {item.status === "uploading" ? (
                            <button
                              type="button"
                              aria-label="取消上传"
                              onClick={() => item.controller?.abort()}
                              className="rounded-md px-1.5 py-0.5 transition-colors hover:bg-white/[0.06]"
                            >
                              取消
                            </button>
                          ) : null}
                          <button
                            type="button"
                            aria-label="上移"
                            disabled={index === 0}
                            onClick={() => moveFile(item.uid, -1)}
                            className="rounded-md px-1 py-0.5 transition-colors hover:bg-white/[0.06] disabled:opacity-40"
                          >
                            ←
                          </button>
                          <button
                            type="button"
                            aria-label="下移"
                            disabled={index === files.length - 1}
                            onClick={() => moveFile(item.uid, 1)}
                            className="rounded-md px-1 py-0.5 transition-colors hover:bg-white/[0.06] disabled:opacity-40"
                          >
                            →
                          </button>
                          <button
                            type="button"
                            aria-label="移除"
                            onClick={() => removeFile(item.uid)}
                            className="rounded-md px-1 py-0.5 transition-colors hover:bg-white/[0.06] hover:text-[var(--danger)]"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : null}

              {anyUploading ? (
                <div className="mt-3 h-1 overflow-hidden rounded-full bg-white/[0.06]">
                  <div
                    className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-200 ease-out"
                    style={{ width: `${totalProgress * 100}%` }}
                  />
                </div>
              ) : null}
            </div>

            <FieldCard label="项目名称" remaining={titleRemaining} max={TITLE_MAX}>
              <input
                value={projectTitle}
                onChange={(event) => setProjectTitle(event.target.value.slice(0, TITLE_MAX))}
                maxLength={TITLE_MAX}
                className="mt-3 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--border-amber)]"
              />
            </FieldCard>

            <FieldCard label="基础参数" remaining={promptRemaining} max={PROMPT_MAX}>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <ParamSelect
                  label="年龄段"
                  value={ageSegment}
                  options={AGE_SEGMENTS}
                  onChange={setAgeSegment}
                />
                <ParamSelect
                  label="性别"
                  value={gender}
                  options={GENDERS}
                  onChange={setGender}
                />
                <ParamSelect
                  label="外貌方向"
                  value={appearanceDirection}
                  options={APPEARANCE_DIRECTIONS}
                  onChange={setAppearanceDirection}
                />
                <ParamSelect
                  label="风格气质"
                  value={styleDirection}
                  options={STYLE_DIRECTIONS}
                  onChange={setStyleDirection}
                />
              </div>
              <textarea
                value={extraPrompt}
                onChange={(event) => setExtraPrompt(event.target.value.slice(0, 120))}
                maxLength={120}
                rows={3}
                placeholder="补充说明（可选），例如：更活泼一点，适合校园通勤"
                className="mt-3 w-full resize-none rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 py-2 text-sm leading-6 text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--border-amber)]"
              />
              <div className="mt-3 rounded-md border border-[var(--border)] bg-white/[0.025] px-3 py-2 text-xs leading-5 text-[var(--fg-2)]">
                将用于后续模特候选和最终图：{composedPrompt}
              </div>
            </FieldCard>

            {error ? (
              <div className="rounded-md border border-[var(--danger)]/30 bg-[var(--danger-soft)] p-3 text-sm text-[var(--fg-0)]">
                <X className="mr-1.5 inline h-4 w-4 align-text-bottom text-[var(--danger)]" />
                {error}
              </div>
            ) : null}

            <Button
              variant="primary"
              size="lg"
              loading={submitting || createMutation.isPending}
              disabled={!files.length}
              onClick={onCreate}
              leftIcon={<WandSparkles className="h-4 w-4" />}
            >
              {allDone ? "创建项目并开始分析" : "上传图片并创建项目"}
            </Button>
          </section>

          <aside className="space-y-3">
            <InfoPanel title="默认闭环">
              <p>商品理解、3 套模特候选、确认模特、4 张展示图、自动质检、一次文字返修。</p>
            </InfoPanel>
            <InfoPanel title="质量策略">
              <p>默认高质量模式，优先模特一致性、商品还原度和高级质感。</p>
            </InfoPanel>
            <InfoPanel title="顺序与主图">
              <p>第一张图作为商品主图。可拖拽 ← / → 调整顺序。</p>
            </InfoPanel>
          </aside>
        </div>
      </main>
    </div>
  );
}

function FieldCard({
  label,
  remaining,
  max,
  children,
}: {
  label: string;
  remaining: number;
  max: number;
  children: React.ReactNode;
}) {
  const usage = (max - remaining) / max;
  const warning = usage > 0.92;
  return (
    <label className="block rounded-md border border-[var(--border)] bg-white/[0.035] p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-[var(--fg-0)]">{label}</span>
        <span
          className={cn(
            "text-[11px] tabular-nums",
            warning ? "text-[var(--warning)]" : "text-[var(--fg-2)]",
          )}
        >
          {Math.max(0, remaining)} / {max}
        </span>
      </div>
      {children}
    </label>
  );
}

function ParamSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly (readonly [string, string])[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="text-xs text-[var(--fg-2)]">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1.5 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--border-amber)]"
      >
        {options.map(([text, optionValue]) => (
          <option key={`${label}-${text}`} value={optionValue}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}
