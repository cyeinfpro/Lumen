"use client";

// Editorial 重构：compact header + hairline section + portrait 商品图卡 + amber CTA。
// 1) Header：mono eyebrow + compact title + minimal breadcrumb
// 2) Upload：hairline section header + dashed dropzone + drag-active amber soft bg
// 3) 商品图列表：aspect 4/5 portrait + 左上 N° + 右上控件 + 底部 mono 元数据 + hairline 进度
// 4) 字段：hairline section header (mono eyebrow + compact title) + 内容直铺
// 5) ParamSelect：mono label + 极简 select + amber focus
// 6) CTA：amber 大圆角 hero 按钮，sticky 底部
//
// 业务逻辑保持不变：uploadWithProgress / XHR / abort / progress / CSRF / API / validation / composedPrompt。

import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Loader2, RotateCcw, Trash2, Upload, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import { useCreateApparelWorkflowMutation } from "@/lib/queries";
import { API_BASE } from "@/lib/apiClient";
import { ensureCsrfToken, refreshCsrfToken } from "@/lib/api/http";
import { cn } from "@/lib/utils";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";
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
  ["熟龄", "熟龄"],
  ["中年", "中年"],
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
}

type PendingFilesUpdater =
  | PendingFile[]
  | ((current: PendingFile[]) => PendingFile[]);

function getUploadSummary(files: PendingFile[]) {
  const fileCount = files.length;
  return {
    allDone: fileCount > 0 && files.every((file) => file.status === "done"),
    anyUploading: files.some((file) => file.status === "uploading"),
    totalProgress: fileCount
      ? files.reduce((acc, file) => acc + file.progress, 0) / fileCount
      : 0,
  };
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
  const uploadOnce = (csrf: string | null): Promise<UploadResult> =>
    new Promise((resolve, reject) => {
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
        } else if (xhr.status === 403 && xhr.responseText.includes("csrf_failed")) {
          reject(new Error("csrf_failed"));
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
      signal.addEventListener("abort", () => xhr.abort(), { once: true });

      xhr.send(fd);
    });

  try {
    return await uploadOnce(await ensureCsrfToken());
  } catch (err) {
    if (err instanceof Error && err.message === "csrf_failed") {
      const fresh = await refreshCsrfToken().catch(() => null);
      if (fresh) return uploadOnce(fresh);
      throw new Error("请求校验失败，请刷新页面后再试");
    }
    throw err;
  }
}

export function ApparelWorkflowNewPage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<PendingFile[]>([]);
  const [projectTitle, setProjectTitle] = useState("服饰模特图");
  const [ageSegment, setAgeSegment] = useState("熟龄");
  const [gender, setGender] = useState("女性");
  const [appearanceDirection, setAppearanceDirection] = useState("");
  const [styleDirection, setStyleDirection] = useState("高级简洁");
  const [extraPrompt, setExtraPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const filesRef = useRef<PendingFile[]>([]);
  const uploadTasksRef = useRef(new Map<string, Promise<string | null>>());
  const submittingRef = useRef(false);
  const updateFiles = useCallback((updater: PendingFilesUpdater) => {
    const next =
      typeof updater === "function" ? updater(filesRef.current) : updater;
    filesRef.current = next;
    setFiles(next);
    return next;
  }, []);

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

  // 释放 ObjectURL 并终止尚未完成的上传，避免卸载后继续回写状态。
  useEffect(() => {
    const uploadTasks = uploadTasksRef.current;
    return () => {
      filesRef.current.forEach((item) => {
        item.controller?.abort();
        URL.revokeObjectURL(item.url);
      });
      filesRef.current = [];
      uploadTasks.clear();
    };
  }, []);

  const validateFile = (file: File): string | null => {
    if (!ACCEPT.includes(file.type)) return `不支持的格式：${file.type || "未知"}`;
    if (file.size > MAX_PRODUCT_IMAGE_BYTES)
      return `单张不能超过 ${formatBytes(MAX_PRODUCT_IMAGE_BYTES)}`;
    return null;
  };

  const addFiles = (incoming: File[]) => {
    setError(null);
    if (submittingRef.current || createMutation.isPending) return;
    const current = filesRef.current;
    const slots = MAX_PRODUCT_IMAGES - current.length;
    if (slots <= 0) {
      toast.warning(`最多 ${MAX_PRODUCT_IMAGES} 张`);
      return;
    }
    const validFiles: File[] = [];
    for (const file of incoming) {
      const reason = validateFile(file);
      if (reason) {
        toast.error(`${file.name}：${reason}`);
        continue;
      }
      validFiles.push(file);
    }
    const acceptedFiles = validFiles.slice(0, slots);
    const next: PendingFile[] = acceptedFiles.map((file) => ({
        uid: uid(),
        file,
        url: URL.createObjectURL(file),
        progress: 0,
        status: "queued",
      }));
    if (next.length) updateFiles([...current, ...next]);
    if (validFiles.length > slots) {
      toast.warning(`最多 ${MAX_PRODUCT_IMAGES} 张，超出 ${validFiles.length - slots} 张已忽略`);
    }
  };

  const onPickFiles = (list: FileList | null) => {
    if (!list) return;
    addFiles(Array.from(list));
  };

  const removeFile = (uidToRemove: string) => {
    const target = filesRef.current.find((item) => item.uid === uidToRemove);
    if (target) {
      target.controller?.abort();
      URL.revokeObjectURL(target.url);
    }
    updateFiles((current) =>
      current.filter((item) => item.uid !== uidToRemove),
    );
  };

  const moveFile = (uidToMove: string, direction: -1 | 1) => {
    updateFiles((current) => {
      const idx = current.findIndex((item) => item.uid === uidToMove);
      if (idx < 0) return current;
      const next = idx + direction;
      if (next < 0 || next >= current.length) return current;
      const copy = [...current];
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

  const uploadOne = useCallback(
    (target: PendingFile): Promise<string | null> => {
      const current = filesRef.current.find((item) => item.uid === target.uid);
      if (!current) return Promise.resolve(null);
      if (current.status === "done" && current.uploadedId) {
        return Promise.resolve(current.uploadedId);
      }
      const existing = uploadTasksRef.current.get(target.uid);
      if (existing) return existing;

      const controller = new AbortController();
      const task = (async () => {
        updateFiles((items) =>
          items.map((item) =>
            item.uid === target.uid
              ? {
                  ...item,
                  status: "uploading",
                  progress: 0,
                  error: undefined,
                  controller,
                }
              : item,
          ),
        );
        try {
          const result = await uploadWithProgress(
            current.file,
            (ratio) =>
              updateFiles((items) =>
                items.map((item) =>
                  item.uid === target.uid && item.controller === controller
                    ? { ...item, progress: ratio }
                    : item,
                ),
              ),
            controller.signal,
          );
          updateFiles((items) =>
            items.map((item) =>
              item.uid === target.uid && item.controller === controller
                ? {
                    ...item,
                    status: "done",
                    progress: 1,
                    uploadedId: result.id,
                    controller: undefined,
                  }
                : item,
            ),
          );
          return result.id;
        } catch (err) {
          const canceled =
            err instanceof DOMException && err.name === "AbortError";
          const message = canceled
            ? "已取消"
            : err instanceof Error
              ? err.message
              : "上传失败";
          updateFiles((items) =>
            items.map((item) =>
              item.uid === target.uid && item.controller === controller
                ? {
                    ...item,
                    status: canceled ? "canceled" : "error",
                    error: message,
                    controller: undefined,
                  }
                : item,
            ),
          );
          return null;
        }
      })();

      uploadTasksRef.current.set(target.uid, task);
      void task.finally(() => {
        if (uploadTasksRef.current.get(target.uid) === task) {
          uploadTasksRef.current.delete(target.uid);
        }
      });
      return task;
    },
    [updateFiles],
  );

  const onCreate = async () => {
    if (submittingRef.current || createMutation.isPending) return;
    setError(null);
    const snapshot = [...filesRef.current];
    if (!snapshot.length) {
      setError(`请上传 1 到 ${MAX_PRODUCT_IMAGES} 张商品图`);
      return;
    }
    if (composedPrompt.length > PROMPT_MAX) {
      setError("基础参数过长，请精简补充说明");
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    try {
      // 把所有未完成的并发上传起来，取每个文件的最终 id
      const results = await Promise.all(
        snapshot.map(async (file) =>
          file.status === "done" && file.uploadedId
            ? file.uploadedId
            : await uploadOne(file),
        ),
      );
      const ids = results.filter((id): id is string => Boolean(id));
      if (ids.length !== snapshot.length) {
        toast.warning("部分图片未能上传，可重试单张或移除后重新创建");
        return;
      }
      const currentOrder = filesRef.current.map((file) => file.uid).join(",");
      const submittedOrder = snapshot.map((file) => file.uid).join(",");
      if (currentOrder !== submittedOrder) {
        toast.warning("图片列表已变化，请确认顺序后重新创建");
        return;
      }
      await createMutation.mutateAsync({
        product_image_ids: ids,
        user_prompt: composedPrompt,
        quality_mode: "premium",
        title: projectTitle.trim() || "服饰模特图",
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建项目失败");
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  const { allDone, anyUploading, totalProgress } = useMemo(
    () => getUploadSummary(files),
    [files],
  );
  const isBusy = submitting || createMutation.isPending;
  const ctaDisabled = !files.length || isBusy;

  return (
    <div className="page-shell relative h-[100dvh] max-md:[&_button]:min-h-[44px] max-md:[&_input]:text-[16px] max-md:[&_textarea]:text-[16px]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="新建"
        subtitle="新建服饰项目"
        backHref="/projects/apparel-model-showcase"
        backLabel="返回服饰模特图"
      />
      <ProjectTopBar />

      <main className="page-scroll lumen-studio-bg project-mobile-scroll-with-cta mb-[var(--mobile-tabbar-height)] touch-pan-y">
        <div className="page-frame grid max-w-[1280px] gap-3">
          <header className="page-header hidden md:grid">
            <div className="page-header-copy">
              <p className="type-page-kicker">
                新建项目
              </p>
              <h1 className="type-page-title">
                新建服饰模特图
              </h1>
              <p className="type-page-subtitle hidden max-w-3xl lg:block">
                上传 1-3 张商品图，确认模特候选后生成 4 张电商展示图。
              </p>
            </div>
            <div className="page-header-actions">
              <Link
                href="/projects/apparel-model-showcase"
                className="inline-flex min-h-9 shrink-0 items-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                返回服饰模特图
              </Link>
            </div>
          </header>

          <div className="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1fr)_280px] lg:gap-8">
            <section className="grid min-w-0 gap-5 md:gap-6">
              {/* Upload */}
              <SectionHeader
                eyebrow="N°01 — 上传"
                title="商品图"
                trailing={
                  <span className="font-mono text-[11px] uppercase tracking-[0.18em] tabular-nums text-[var(--fg-2)]">
                    {String(files.length).padStart(2, "0")} / {String(MAX_PRODUCT_IMAGES).padStart(2, "0")}
                  </span>
                }
              />

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
                  if (event.currentTarget.contains(event.relatedTarget as Node)) return;
                  setDragActive(false);
                }}
                onDrop={onDrop}
                className="relative -mt-3 md:-mt-4"
              >
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isBusy}
                  className={cn(
                    "flex min-h-[188px] w-full cursor-pointer flex-col items-center justify-center gap-3 border border-dashed px-3 text-center transition-[background-color,border-color] duration-[var(--dur-base)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 disabled:cursor-not-allowed disabled:opacity-60 sm:min-h-[220px] md:min-h-[260px]",
                    dragActive
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)]"
                      : "border-[var(--border-strong)] hover:border-[var(--border-amber)]/50 hover:bg-white/[0.02]",
                  )}
                >
                  <span
                    className={cn(
                      "inline-flex h-11 w-11 items-center justify-center rounded-full border transition-colors md:h-12 md:w-12",
                      dragActive
                        ? "border-[var(--border-amber)] bg-[var(--accent)] text-black"
                        : "border-[var(--border)] bg-transparent text-[var(--fg-1)]",
                    )}
                  >
                    <Upload className="h-5 w-5" strokeWidth={1.5} />
                  </span>
                  <p
                    className={cn(
                      "type-section-title md:text-[20px]",
                      dragActive ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
                    )}
                  >
                    {dragActive ? "松开即可加入项目" : "拖拽到这里，或点击选择"}
                  </p>
                  <p className="max-w-[18rem] font-mono text-[10px] uppercase tracking-[0.12em] leading-5 text-[var(--fg-2)] sm:tracking-[0.22em]">
                    PNG / JPG / WebP 格式 &nbsp;·&nbsp; ≤ {formatBytes(MAX_PRODUCT_IMAGE_BYTES)} &nbsp;·&nbsp; 最多 {MAX_PRODUCT_IMAGES} 张
                  </p>
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

              {/* Aggregate progress hairline */}
              {anyUploading ? (
                <div className="-mt-6 grid gap-2">
                  <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                    <span>上传中</span>
                    <span className="tabular-nums text-[var(--amber-300)]">
                      {Math.round(totalProgress * 100).toString().padStart(2, "0")}%
                    </span>
                  </div>
                  <div className="relative h-px w-full bg-[var(--border)]">
                    <div
                      className="absolute inset-y-0 left-0 bg-[var(--amber-400)] transition-[width] duration-200 ease-out"
                      style={{ width: `${totalProgress * 100}%` }}
                    />
                  </div>
                </div>
              ) : null}

              {/* File preview grid: portrait cards */}
              {files.length > 0 ? (
                <ul className="-mt-4 grid grid-cols-1 gap-x-4 gap-y-8 min-[390px]:grid-cols-2 md:grid-cols-3 md:gap-x-6">
                  {files.map((item, index) => (
                    <FilePortrait
                      key={item.uid}
                      item={item}
                      index={index}
                      total={files.length}
                      locked={isBusy}
                      onRetry={() => uploadOne(item)}
                      onCancel={() => item.controller?.abort()}
                      onMoveUp={() => moveFile(item.uid, -1)}
                      onMoveDown={() => moveFile(item.uid, 1)}
                      onRemove={() => removeFile(item.uid)}
                    />
                  ))}
                </ul>
              ) : null}

              {/* Project title */}
              <div className="grid gap-4">
                <SectionHeader
                  eyebrow="N°02 — 标题"
                  title="项目名称"
                  trailing={
                    <CharCount remaining={titleRemaining} max={TITLE_MAX} />
                  }
                />
                <input
                  value={projectTitle}
                  onChange={(event) => setProjectTitle(event.target.value.slice(0, TITLE_MAX))}
                  maxLength={TITLE_MAX}
                  aria-label="项目名称"
                  className="-mt-2 h-12 w-full border-b border-[var(--border)] bg-transparent px-1 text-[16px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                  placeholder="给这个项目起个名字"
                />
              </div>

              {/* Settings */}
              <div className="grid gap-5">
                <SectionHeader
                  eyebrow="N°03 — 基本参数"
                  title="基础参数"
                  trailing={
                    <CharCount remaining={promptRemaining} max={PROMPT_MAX} />
                  }
                />
                <div className="-mt-2 grid gap-x-8 gap-y-6 md:grid-cols-2">
                  <ParamSelect
                    label="年龄段"
                    chineseLabel="年龄段"
                    value={ageSegment}
                    options={AGE_SEGMENTS}
                    onChange={setAgeSegment}
                  />
                  <ParamSelect
                    label="性别"
                    chineseLabel="性别"
                    value={gender}
                    options={GENDERS}
                    onChange={setGender}
                  />
                </div>

                <details className="border-t border-[var(--border-subtle)] pt-4">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-[13px] text-[var(--fg-1)]">
                    <span>
                      <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
                        高级偏好
                      </span>
                      <span className="ml-2 text-[12px] text-[var(--fg-3)]">
                        外貌、风格与补充说明
                      </span>
                    </span>
                    <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
                      可选
                    </span>
                  </summary>
                  <div className="mt-4 grid gap-x-8 gap-y-6 md:grid-cols-2">
                    <ParamSelect
                      label="外貌方向"
                      chineseLabel="外貌方向"
                      value={appearanceDirection}
                      options={APPEARANCE_DIRECTIONS}
                      onChange={setAppearanceDirection}
                    />
                    <ParamSelect
                      label="风格"
                      chineseLabel="风格气质"
                      value={styleDirection}
                      options={STYLE_DIRECTIONS}
                      onChange={setStyleDirection}
                    />
                    <label className="grid gap-2 md:col-span-2">
                      <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                        Notes <span className="ml-1 normal-case tracking-normal text-[var(--fg-3)]">补充说明</span>
                      </span>
                      <textarea
                        value={extraPrompt}
                        onChange={(event) => setExtraPrompt(event.target.value.slice(0, 120))}
                        maxLength={120}
                        rows={3}
                        aria-label="补充说明"
                        placeholder="例如：更活泼一点，适合校园通勤"
                        className="w-full resize-none border-b border-[var(--border)] bg-transparent px-1 py-2 text-[16px] leading-[1.6] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] md:text-[15px]"
                      />
                    </label>
                  </div>
                </details>

                <div className="min-w-0 border-t border-[var(--border)] pt-4">
                  <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                    组合提示词
                  </p>
                  <p className="mt-2 break-words text-[13px] leading-[1.7] text-[var(--fg-1)]">
                    {composedPrompt}
                  </p>
                </div>
              </div>

              {error ? (
                <div
                  role="alert"
                  className="border-y border-[var(--danger)]/30 bg-[var(--danger-soft)]/30 px-4 py-4 md:px-5"
                >
                  <div className="flex items-start gap-3">
                    <X className="mt-0.5 h-4 w-4 shrink-0 text-[var(--danger)]" />
                    <div className="min-w-0">
                      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--danger)]">
                        错误
                      </p>
                      <p className="mt-1 text-[13px] text-[var(--fg-0)]">{error}</p>
                    </div>
                  </div>
                </div>
              ) : null}

              {/* Desktop CTA inline at the bottom */}
              <div className="hidden border-t border-[var(--border)] pt-6 md:block">
                <button
                  type="button"
                  onClick={onCreate}
                  disabled={ctaDisabled}
                  className={cn(
                    "group inline-flex items-center gap-3 rounded-full px-7 py-3.5 font-medium text-black shadow-[var(--shadow-amber)] transition-[transform,opacity,box-shadow] duration-[var(--dur-base)]",
                    ctaDisabled
                      ? "cursor-not-allowed bg-[var(--fg-3)] opacity-60"
                      : "cursor-pointer bg-[var(--accent)] hover:scale-[1.02] active:scale-[0.98]",
                  )}
                >
                  {isBusy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : null}
                  <span>
                    {isBusy
                      ? "正在创建项目"
                      : allDone
                        ? "创建项目并开始分析"
                        : "上传图片并创建项目"}
                  </span>
                  {!isBusy ? (
                    <ArrowRight className="h-4 w-4 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-enabled:group-hover:translate-x-0 group-enabled:group-hover:opacity-100" />
                  ) : null}
                </button>
              </div>
            </section>

            {/* Right rail */}
            <aside className="hidden grid-cols-1 gap-0 self-start lg:grid">
              <InfoPanel title="流程">
                <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
                  商品约束、3 套模特候选、配饰四宫格、4 张展示图、一次文字返修。
                </p>
              </InfoPanel>
              <InfoPanel title="质量">
                <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
                  默认高质量模式，优先模特一致性、商品还原度和高级质感。
                </p>
              </InfoPanel>
              <InfoPanel title="顺序">
                <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
                  第一张图作为商品主图。可用上移 / 下移调整顺序。
                </p>
              </InfoPanel>
            </aside>
          </div>
        </div>
      </main>

      {/* Mobile sticky CTA */}
      <div className="fixed inset-x-0 bottom-[var(--mobile-tabbar-height)] z-30 border-t border-[var(--border)] bg-[var(--bg-0)]/95 px-3 py-3 backdrop-blur-xl min-[390px]:px-4 md:hidden">
        <button
          type="button"
          onClick={onCreate}
          disabled={ctaDisabled}
          className={cn(
            "inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-full px-5 py-3 text-[15px] font-medium text-black transition-[opacity,transform] duration-[var(--dur-base)]",
            ctaDisabled
              ? "cursor-not-allowed bg-[var(--fg-3)] opacity-60"
              : "cursor-pointer bg-[var(--accent)] shadow-[var(--shadow-amber)] active:scale-[0.98]",
          )}
        >
          {isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
          {isBusy
            ? "正在创建项目"
            : allDone
              ? "创建项目并开始分析"
              : "上传图片并创建项目"}
        </button>
      </div>

      <ProjectMobileTabBar />
    </div>
  );
}

// hairline section header：mono eyebrow + compact title + 可选右侧元素
function SectionHeader({
  eyebrow,
  title,
  trailing,
}: {
  eyebrow: string;
  title: string;
  trailing?: React.ReactNode;
}) {
  return (
    <header className="border-t border-[var(--border)] pt-5">
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="type-page-kicker">
            {eyebrow}
          </p>
          <h2 className="type-section-title mt-2 md:text-[22px]">
            {title}
          </h2>
        </div>
        {trailing ? <div className="shrink-0 self-end pb-1.5">{trailing}</div> : null}
      </div>
    </header>
  );
}

function CharCount({ remaining, max }: { remaining: number; max: number }) {
  const usage = (max - remaining) / max;
  const warning = usage > 0.92;
  return (
    <span
      className={cn(
        "font-mono text-[10px] uppercase tracking-[0.22em] tabular-nums",
        warning ? "text-[var(--warning)]" : "text-[var(--fg-2)]",
      )}
    >
      {Math.max(0, remaining)} / {max}
    </span>
  );
}

// Portrait 商品图卡：4/5 大图 + N° 序号 + 控件 + mono 元数据
function FilePortrait({
  item,
  index,
  total,
  locked,
  onRetry,
  onCancel,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  item: PendingFile;
  index: number;
  total: number;
  locked: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  const isMain = index === 0;
  const num = `N°${String(index + 1).padStart(2, "0")}`;
  const presentation = pendingFilePresentation(item.status);

  return (
    <li className="group relative">
      <div
        className={cn(
          "relative aspect-[4/5] overflow-hidden border bg-[var(--bg-2)] transition-colors duration-[var(--dur-base)]",
          presentation.borderClass,
        )}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={item.url}
          alt={item.file.name}
          className="h-full w-full object-cover"
        />

        {/* gradient for legibility */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-20 bg-gradient-to-b from-black/55 to-transparent"
        />

        {/* uploading overlay */}
        {item.status === "uploading" ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/55 backdrop-blur-sm">
            <Loader2 className="h-5 w-5 animate-spin text-white" />
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] tabular-nums text-white">
              {Math.round(item.progress * 100).toString().padStart(2, "0")}%
            </p>
          </div>
        ) : null}

        {/* error overlay */}
        {item.status === "error" ? (
          <div
            role="alert"
            className="absolute inset-x-0 bottom-0 bg-[var(--danger)]/90 px-3 py-2"
          >
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-white/90">
              失败
            </p>
            <p className="mt-1 line-clamp-2 text-[12px] leading-[1.4] text-white">
              {item.error}
            </p>
          </div>
        ) : null}

        {/* top-left N° + main chip */}
        <div className="absolute left-3 top-3 flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-white/85 mix-blend-difference">
            {num}
          </span>
          {isMain ? (
            <span className="rounded-full bg-[var(--accent)] px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.22em] text-black">
              主图
            </span>
          ) : null}
        </div>

        {/* top-right controls */}
        <FilePortraitControls
          status={item.status}
          index={index}
          total={total}
          locked={locked}
          onRetry={onRetry}
          onCancel={onCancel}
          onMoveUp={onMoveUp}
          onMoveDown={onMoveDown}
          onRemove={onRemove}
        />
      </div>

      {/* meta row */}
      <div className="mt-3 flex min-w-0 items-baseline justify-between gap-3 font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        <span className={cn("truncate", presentation.textClass)} title={item.file.name}>
          {presentation.label}
        </span>
        <span className="shrink-0 tabular-nums">{formatBytes(item.file.size)}</span>
      </div>
      <p
        className="mt-1 truncate text-[12px] text-[var(--fg-1)]"
        title={item.file.name}
      >
        {item.file.name}
      </p>
    </li>
  );
}

function pendingFilePresentation(status: PendingFile["status"]) {
  switch (status) {
    case "uploading":
      return {
        label: "上传中",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--amber-300)]",
      };
    case "done":
      return {
        label: "已就绪",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--success)]",
      };
    case "error":
      return {
        label: "失败",
        borderClass: "border-[var(--danger)]/40",
        textClass: "text-[var(--danger)]",
      };
    case "canceled":
      return {
        label: "已取消",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--fg-2)]",
      };
    default:
      return {
        label: "排队中",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--fg-2)]",
      };
  }
}

function FilePortraitControls({
  status,
  index,
  total,
  locked,
  onRetry,
  onCancel,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  status: PendingFile["status"];
  index: number;
  total: number;
  locked: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="absolute inset-x-2 top-2 flex justify-end opacity-100 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100 focus-within:opacity-100 md:inset-x-auto md:right-2 md:opacity-0">
      <div className="flex gap-0.5 rounded-full border border-white/15 bg-black/55 p-0.5 backdrop-blur md:flex-col md:gap-1 md:p-1">
        <IconBtn label="上移" onClick={onMoveUp} disabled={locked || index === 0}>
          <ArrowUp className="h-3.5 w-3.5" />
        </IconBtn>
        <IconBtn label="下移" onClick={onMoveDown} disabled={locked || index === total - 1}>
          <ArrowDown className="h-3.5 w-3.5" />
        </IconBtn>
        {status === "error" ? (
          <span aria-live="polite" className="contents">
            <IconBtn label="重试" onClick={onRetry} disabled={locked}>
              <RotateCcw className="h-3.5 w-3.5" />
            </IconBtn>
          </span>
        ) : null}
        {status === "uploading" ? (
          <IconBtn label="取消" onClick={onCancel}>
            <X className="h-3.5 w-3.5" />
          </IconBtn>
        ) : null}
        <IconBtn label="移除" onClick={onRemove} disabled={locked} danger>
          <Trash2 className="h-3.5 w-3.5" />
        </IconBtn>
      </div>
    </div>
  );
}

function IconBtn({
  label,
  onClick,
  disabled,
  danger,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-11 w-11 items-center justify-center rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-30 md:h-8 md:w-8",
        danger
          ? "text-white/85 hover:bg-[var(--danger)]/70 hover:text-white"
          : "text-white/85 hover:bg-white/15 hover:text-white",
      )}
    >
      {children}
    </button>
  );
}

function ParamSelect({
  label,
  chineseLabel,
  value,
  options,
  onChange,
}: {
  label: string;
  chineseLabel: string;
  value: string;
  options: readonly (readonly [string, string])[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="block min-w-0">
      <span className="flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        <span>{label}</span>
        {chineseLabel && chineseLabel !== label ? (
          <span className="normal-case tracking-normal text-[var(--fg-3)]">
            {chineseLabel}
          </span>
        ) : null}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-2 h-11 w-full appearance-none border-b border-[var(--border)] bg-transparent bg-[length:14px_14px] bg-[right_4px_center] bg-no-repeat pl-1 pr-6 text-[16px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] md:h-10 md:text-[15px]"
        style={{
          backgroundImage:
            "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 14 14' fill='none' stroke='%23999' stroke-width='1.5'%3E%3Cpath d='M3 5l4 4 4-4'/%3E%3C/svg%3E\")",
        }}
      >
        {options.map(([text, optionValue]) => (
          <option key={`${label}-${text}`} value={optionValue} className="bg-[var(--bg-1)]">
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}
