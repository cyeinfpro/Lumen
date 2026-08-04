"use client";

// 海报项目创建页（mirror ApparelWorkflowNewPage）：
// - 文案 textarea（≤10000）+ 字数计数
// - 风格选择器：弹窗 PosterStyleSelector
// - 目标尺寸 chip 多选（默认 1:1 / 9:16 / 16:9 / 3:4）
// - 品牌资产（折叠）：logo / 产品图 / 主色 / 字体
// - 质量模式 toggle
// - 标题（可选，默认从文案抽取）
//
// 业务逻辑：图片走 uploadWithProgress（XHR + abort）；提交调 createPosterDesignWorkflow。

import {
  ArrowLeft,
  ChevronDown,
  Loader2,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { Dispatch, SetStateAction } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import { useCreatePosterDesignWorkflowMutation } from "@/lib/queries";
import {
  API_BASE,
  type PosterAspectRatio,
  type PosterStyleItem,
} from "@/lib/apiClient";
import { ensureCsrfToken, refreshCsrfToken } from "@/lib/api/http";
import { assertConfirmedIdentityResponse, bindConfirmedIdentityXhr, coordinateIdentityMismatchResponse } from "@/lib/auth/identityPolicy";
import { cn } from "@/lib/utils";
import { OnlineBanner } from "./components/OnlineBanner";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "./components/ProjectTopBar";
import { PosterStyleSelector } from "./components/PosterStyleSelector";
import { InfoPanel } from "./components/StageFrame";
import { POSTER_ASPECT_LABELS, POSTER_DEFAULT_TARGET_ASPECTS } from "./types";
import { formatBytes } from "./utils";
const COPY_MAX = 10000;
const TITLE_MAX = 60;
const MAX_BRAND_IMAGE_BYTES = 12 * 1024 * 1024;
const ACCEPT = ["image/png", "image/jpeg", "image/webp"];

interface UploadResult {
  id: string;
  width: number;
  height: number;
  url: string;
  mime?: string;
}

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
      const identity = bindConfirmedIdentityXhr(xhr, "/images/upload");
      if (csrf) xhr.setRequestHeader("x-csrf-token", csrf);

      const fd = new FormData();
      fd.append("file", file);

      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            assertConfirmedIdentityResponse(identity);
            resolve(JSON.parse(xhr.responseText) as UploadResult);
          } catch {
            reject(new Error("响应解析失败"));
          }
        } else if (coordinateIdentityMismatchResponse(xhr.status, xhr.responseText)) {
          reject(new Error("登录身份已变化，重新操作"));
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
      xhr.onerror = () => reject(new Error("网络错误，检查连接"));
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
      throw new Error("请求校验失败，刷新页面后再试");
    }
    throw err;
  }
}

interface BrandImageState {
  url: string;
  id: string;
  filename: string;
  size: number;
}

type BrandImageKind = "logo" | "product";
type BrandImageSetter = Dispatch<SetStateAction<BrandImageState | null>>;

function brandImageValidationError(file: File): string | null {
  if (!ACCEPT.includes(file.type)) {
    return `不支持的格式：${file.type || "未知"}`;
  }
  if (file.size > MAX_BRAND_IMAGE_BYTES) {
    return `单张不能超过 ${formatBytes(MAX_BRAND_IMAGE_BYTES)}`;
  }
  return null;
}

function setBrandImageState(
  kind: BrandImageKind,
  value: BrandImageState | null,
  setLogo: BrandImageSetter,
  setProduct: BrandImageSetter,
) {
  if (kind === "logo") setLogo(value);
  else setProduct(value);
}

function replaceBrandPreviewUrl(
  kind: BrandImageKind,
  localUrl: string,
  urls: Record<BrandImageKind, string | null>,
  setLogo: BrandImageSetter,
  setProduct: BrandImageSetter,
) {
  if (urls[kind]) URL.revokeObjectURL(urls[kind]);
  urls[kind] = localUrl;
  setBrandImageState(kind, null, setLogo, setProduct);
}

function releaseBrandPreviewUrl(
  kind: BrandImageKind,
  localUrl: string,
  urls: Record<BrandImageKind, string | null>,
) {
  if (urls[kind] !== localUrl) return;
  URL.revokeObjectURL(localUrl);
  urls[kind] = null;
}

function getPosterFormState({
  aspects,
  copy,
  createPending,
  style,
  submitting,
  title,
  uploadPending,
}: {
  aspects: string[];
  copy: string;
  createPending: boolean;
  style: PosterStyleItem | null;
  submitting: boolean;
  title: string;
  uploadPending: boolean;
}) {
  const copyTrimmed = copy.trim();
  const titleTrimmed = title.trim();
  return {
    copyTrimmed,
    derivedTitle:
      titleTrimmed ||
      (copyTrimmed ? copyTrimmed.split(/\n/)[0]?.slice(0, 24) || "海报设计" : "海报设计"),
    ctaBlocked:
      !copyTrimmed ||
      !style ||
      !aspects.length ||
      createPending ||
      submitting ||
      uploadPending,
  };
}

export function PosterWorkflowNewPage() {
  const router = useRouter();
  const [copy, setCopy] = useState("");
  const [title, setTitle] = useState("");
  const [style, setStyle] = useState<PosterStyleItem | null>(null);
  const [aspects, setAspects] = useState<string[]>([
    ...POSTER_DEFAULT_TARGET_ASPECTS,
  ]);
  const [qualityMode, setQualityMode] = useState<"standard" | "premium">("premium");
  const [styleOpen, setStyleOpen] = useState(false);
  const [brandOpen, setBrandOpen] = useState(false);
  const [logo, setLogo] = useState<BrandImageState | null>(null);
  const [product, setProduct] = useState<BrandImageState | null>(null);
  const [primaryColor, setPrimaryColor] = useState<string>("");
  const [fontFamily, setFontFamily] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState<Record<BrandImageKind, boolean>>({
    logo: false,
    product: false,
  });
  const [uploadProgress, setUploadProgress] = useState<
    Record<BrandImageKind, number>
  >({
    logo: 0,
    product: 0,
  });

  const brandUrlRef = useRef<Record<BrandImageKind, string | null>>({
    logo: null,
    product: null,
  });
  const uploadRequestRef = useRef<Record<BrandImageKind, number>>({
    logo: 0,
    product: 0,
  });
  const uploadControllerRef = useRef<
    Record<BrandImageKind, AbortController | null>
  >({
    logo: null,
    product: null,
  });
  // 同步提交锁：防双击 / 重入（与 ApparelWorkflowNewPage 一致）
  const submittingRef = useRef(false);
  useEffect(() => {
    const uploadControllers = uploadControllerRef.current;
    const brandUrls = brandUrlRef.current;
    return () => {
      uploadControllers.logo?.abort();
      uploadControllers.product?.abort();
      if (brandUrls.logo) URL.revokeObjectURL(brandUrls.logo);
      if (brandUrls.product) URL.revokeObjectURL(brandUrls.product);
    };
  }, []);

  const create = useCreatePosterDesignWorkflowMutation({
    onError: (err) =>
      toast.error("创建项目失败", {
        description: err instanceof Error ? err.message : "稍后重试",
      }),
    onSuccess: (out) => {
      toast.success("项目已创建");
      router.push(`/projects/${out.workflow_run_id}`);
    },
  });

  const copyRemaining = COPY_MAX - copy.length;
  const titleRemaining = TITLE_MAX - title.length;

  const toggleAspect = (value: string) => {
    setAspects((prev) =>
      prev.includes(value)
        ? prev.filter((item) => item !== value)
        : [...prev, value],
    );
  };

  const onPickBrandImage = useCallback(
    async (kind: BrandImageKind, file: File) => {
      if (submittingRef.current || create.isPending) return;
      const validationError = brandImageValidationError(file);
      if (validationError) {
        toast.error(validationError);
        return;
      }

      uploadControllerRef.current[kind]?.abort();
      const requestId = uploadRequestRef.current[kind] + 1;
      uploadRequestRef.current[kind] = requestId;
      const controller = new AbortController();
      uploadControllerRef.current[kind] = controller;

      const localUrl = URL.createObjectURL(file);
      replaceBrandPreviewUrl(
        kind,
        localUrl,
        brandUrlRef.current,
        setLogo,
        setProduct,
      );
      setUploading((current) => ({ ...current, [kind]: true }));
      setUploadProgress((current) => ({ ...current, [kind]: 0 }));

      try {
        const out = await uploadWithProgress(
          file,
          (ratio) => {
            if (uploadRequestRef.current[kind] !== requestId) return;
            setUploadProgress((current) => ({
              ...current,
              [kind]: ratio,
            }));
          },
          controller.signal,
        );
        if (uploadRequestRef.current[kind] !== requestId) return;
        const value: BrandImageState = {
          url: localUrl,
          id: out.id,
          filename: file.name,
          size: file.size,
        };
        setBrandImageState(kind, value, setLogo, setProduct);
      } catch (err) {
        if (uploadRequestRef.current[kind] !== requestId) return;
        const canceled =
          err instanceof DOMException && err.name === "AbortError";
        if (!canceled) {
          toast.error("上传失败", {
            description: err instanceof Error ? err.message : "稍后重试",
          });
        }
        releaseBrandPreviewUrl(kind, localUrl, brandUrlRef.current);
      } finally {
        if (uploadRequestRef.current[kind] !== requestId) return;
        uploadControllerRef.current[kind] = null;
        setUploading((current) => ({ ...current, [kind]: false }));
        setUploadProgress((current) => ({ ...current, [kind]: 0 }));
      }
    },
    [create.isPending],
  );

  const removeBrandImage = (kind: BrandImageKind) => {
    uploadRequestRef.current[kind] += 1;
    uploadControllerRef.current[kind]?.abort();
    uploadControllerRef.current[kind] = null;
    setUploading((current) => ({ ...current, [kind]: false }));
    setUploadProgress((current) => ({ ...current, [kind]: 0 }));
    const localUrl = brandUrlRef.current[kind];
    if (localUrl) releaseBrandPreviewUrl(kind, localUrl, brandUrlRef.current);
    setBrandImageState(kind, null, setLogo, setProduct);
  };

  const uploadPending = uploading.logo || uploading.product;
  const { copyTrimmed, derivedTitle, ctaBlocked } = useMemo(
    () =>
      getPosterFormState({
        aspects,
        copy,
        createPending: create.isPending,
        style,
        submitting,
        title,
        uploadPending,
      }),
    [aspects, copy, create.isPending, style, submitting, title, uploadPending],
  );

  const onCreate = async () => {
    if (submittingRef.current || create.isPending) return;
    setError(null);
    if (!copyTrimmed) {
      setError("输入海报文案");
      return;
    }
    if (!style) {
      setError("选择海报风格");
      return;
    }
    if (!aspects.length) {
      setError("至少选择一个目标尺寸");
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    try {
      await create.mutateAsync({
        copy_text: copyTrimmed,
        style_id: style.id,
        target_aspects: aspects as PosterAspectRatio[],
        brand_assets: {
          logo_image_id: logo?.id || null,
          product_image_id: product?.id || null,
          primary_color: primaryColor.trim() || null,
          font_family: fontFamily.trim() || null,
        },
        quality_mode: qualityMode,
        title: derivedTitle,
      });
    } catch {
      // 失败提示由 mutation onError 的 toast 承担，锁在 finally 释放以便重试
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <div className="page-shell relative h-[100dvh] max-md:[&_button]:min-h-[44px] type-body ">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="新建"
        subtitle="新建海报项目"
        backHref="/projects"
        backLabel="返回项目"
      />
      <ProjectTopBar />

      <main className="page-scroll lumen-studio-bg project-mobile-scroll-with-cta mb-[var(--mobile-tabbar-height)]">
        <div className="page-frame grid max-w-[1280px] gap-3">
          <header className="page-header hidden md:grid">
            <div className="page-header-copy">
              <p className="type-page-kicker">新建项目</p>
              <h1 className="type-page-title">新建海报设计</h1>
              <p className="type-page-subtitle hidden max-w-3xl lg:block">
                录入文案、选择风格、确认尺寸；剩下交给 AI。
              </p>
            </div>
            <div className="page-header-actions">
              <Link
                href="/projects"
                className="inline-flex min-h-9 shrink-0 items-center gap-1.5 border border-[var(--border)] px-3 type-caption text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                返回项目
              </Link>
            </div>
          </header>

          <div className="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1fr)_280px] lg:gap-8">
            <section className="grid min-w-0 gap-5 md:gap-6">
              {/* Copy */}
              <SectionHeader
                eyebrow="N°01 — 文案"
                title="海报文案"
                trailing={
                  <CharCount remaining={copyRemaining} max={COPY_MAX} />
                }
              />
              <textarea
                value={copy}
                onChange={(event) => setCopy(event.target.value.slice(0, COPY_MAX))}
                rows={6}
                maxLength={COPY_MAX}
                placeholder={
                  "例如：\n夏季新品·椰子香水\n清新调，海洋木质底；525 ml 经典瓶身\n限时五折 · 立即下单"
                }
                className="control-shell -mt-3 w-full resize-y px-3 py-2 type-body leading-7 text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)]"
              />

              <PosterStyleSection
                style={style}
                onOpen={() => setStyleOpen(true)}
                onClear={() => setStyle(null)}
              />

              {/* Aspects */}
              <SectionHeader
                eyebrow="N°03 — 尺寸"
                title="目标尺寸"
                trailing={
                  <span className="type-caption text-[var(--fg-2)]">
                    {String(aspects.length).padStart(2, "0")} / {String(POSTER_ASPECT_LABELS.length).padStart(2, "0")}
                  </span>
                }
              />
              <div className="-mt-2 flex flex-wrap gap-2">
                {POSTER_ASPECT_LABELS.map(([value, label]) => {
                  const active = aspects.includes(value);
                  return (
                    <button
                      key={value}
                      type="button"
                      onClick={() => toggleAspect(value)}
                      className={cn(
                        "inline-flex min-h-11 cursor-pointer items-center rounded-full border px-3 type-caption transition-colors md:min-h-9",
                        active
                          ? "border-accent-border bg-[var(--accent-soft)] text-accent"
                          : "border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)] hover:text-[var(--fg-0)]",
                      )}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>

              {/* Brand assets (collapsible) */}
              <details
                open={brandOpen}
                onToggle={(event) =>
                  setBrandOpen((event.currentTarget as HTMLDetailsElement).open)
                }
                className="border-t border-[var(--border)] pt-5"
              >
                <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-left">
                  <div className="min-w-0">
                    <p className="type-page-kicker">N°04 — 品牌（可选）</p>
                    <h2 className="type-section-title mt-2 ">
                      品牌素材
                    </h2>
                  </div>
                  <ChevronDown
                    className={cn(
                      "h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform",
                      brandOpen && "rotate-180",
                    )}
                  />
                </summary>

                <div className="mt-4 grid gap-5 md:grid-cols-2">
                  <BrandImageSlot
                    label="Logo"
                    state={logo}
                    uploading={uploading.logo}
                    progress={uploadProgress.logo}
                    onPick={(file) => onPickBrandImage("logo", file)}
                    onRemove={() => removeBrandImage("logo")}
                  />
                  <BrandImageSlot
                    label="产品图"
                    state={product}
                    uploading={uploading.product}
                    progress={uploadProgress.product}
                    onPick={(file) => onPickBrandImage("product", file)}
                    onRemove={() => removeBrandImage("product")}
                  />
                </div>

                <div className="mt-5 grid gap-x-8 gap-y-5 md:grid-cols-2">
                  <label className="block min-w-0">
                    <span className="type-caption text-[var(--fg-2)]">
                      主色
                    </span>
                    <div className="mt-2 flex min-w-0 items-center gap-3">
                      <input
                        type="color"
                        value={primaryColor || "#ffd166"}
                        onChange={(event) => setPrimaryColor(event.target.value)}
                        className="h-11 w-12 cursor-pointer border border-[var(--border)] bg-transparent md:h-9"
                      />
                      <input
                        value={primaryColor}
                        onChange={(event) =>
                          setPrimaryColor(event.target.value.slice(0, 24))
                        }
                        maxLength={24}
                        placeholder="#FFD166 / amber"
                        className="control-shell h-11 min-w-0 flex-1 px-3 type-body text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
                      />
                    </div>
                  </label>
                  <label className="block min-w-0">
                    <span className="type-caption text-[var(--fg-2)]">
                      字体
                    </span>
                    <input
                      value={fontFamily}
                      onChange={(event) =>
                        setFontFamily(event.target.value.slice(0, 64))
                      }
                      maxLength={64}
                      placeholder="例如：思源黑体 / Inter"
                      className="control-shell mt-2 h-11 w-full px-3 type-body text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
                    />
                  </label>
                </div>
              </details>

              {/* Quality + title */}
              <SectionHeader eyebrow="N°05 — 设置" title="项目设置" />
              <div className="-mt-2 grid gap-5 md:grid-cols-2">
                <label className="block min-w-0">
                  <span className="type-caption text-[var(--fg-2)]">
                    标题
                  </span>
                  <input
                    value={title}
                    onChange={(event) =>
                      setTitle(event.target.value.slice(0, TITLE_MAX))
                    }
                    maxLength={TITLE_MAX}
                    placeholder={derivedTitle}
                    className="control-shell mt-2 h-11 w-full px-3 type-body text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] placeholder:text-[var(--fg-3)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-10"
                  />
                  <CharCount remaining={titleRemaining} max={TITLE_MAX} />
                </label>
                <div>
                  <span className="type-caption text-[var(--fg-2)]">
                    质量
                  </span>
                  <div className="mt-2 inline-flex rounded-full border border-[var(--border)] p-0.5">
                    <button
                      type="button"
                      onClick={() => setQualityMode("standard")}
                      className={cn(
                        "inline-flex min-h-11 items-center rounded-full px-3 type-caption transition-colors md:min-h-9",
                        qualityMode === "standard"
                          ? "bg-accent text-[var(--accent-on)]"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                      )}
                    >
                      标准
                    </button>
                    <button
                      type="button"
                      onClick={() => setQualityMode("premium")}
                      className={cn(
                        "inline-flex min-h-11 items-center rounded-full px-3 type-caption transition-colors md:min-h-9",
                        qualityMode === "premium"
                          ? "bg-accent text-[var(--accent-on)]"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                      )}
                    >
                      高质量
                    </button>
                  </div>
                </div>
              </div>

              {error ? (
                <div
                  role="alert"
                  className="border-y border-danger-border bg-danger-soft px-4 py-4 md:px-5"
                >
                  <div className="flex items-start gap-3">
                    <X className="mt-0.5 h-4 w-4 shrink-0 text-[var(--danger)]" />
                    <div>
                      <p className="type-caption text-[var(--danger)]">
                        错误
                      </p>
                      <p className="mt-1 type-body-sm text-[var(--fg-0)]">{error}</p>
                    </div>
                  </div>
                </div>
              ) : null}

              <PosterCreateButton
                pending={submitting || create.isPending}
                disabled={ctaBlocked}
                onClick={onCreate}
              />
            </section>

            <aside className="hidden grid-cols-1 gap-0 self-start lg:grid">
              <InfoPanel title="流程">
                <p className="type-body-sm leading-[1.7] text-[var(--fg-1)]">
                  文案切分、4 张母版候选、多尺寸成品（默认 4 尺寸），可逐张返修。
                </p>
              </InfoPanel>
              <InfoPanel title="文字策略">
                <p className="type-body-sm leading-[1.7] text-[var(--fg-1)]">
                  V1 全 AI 出图（无文字层 Canvas 编辑器）。所有文字直接写在 prompt 里。
                </p>
              </InfoPanel>
              <InfoPanel title="风格">
                <p className="type-body-sm leading-[1.7] text-[var(--fg-1)]">
                  在「风格库」沉淀常用风格，每次创建项目只挑一个；保证视觉一致性。
                </p>
              </InfoPanel>
            </aside>
          </div>
        </div>
      </main>

      <PosterCreateButton
        mobile
        pending={submitting || create.isPending}
        disabled={ctaBlocked}
        onClick={onCreate}
      />

      <ProjectMobileTabBar />

      <PosterStyleSelector
        open={styleOpen}
        selectedId={style?.id}
        onClose={() => setStyleOpen(false)}
        onSelect={(item) => {
          setStyle(item);
          setStyleOpen(false);
        }}
      />
    </div>
  );
}

import {
  CharCount,
  PosterCreateButton,
  PosterStyleSection,
  SectionHeader,
} from "./PosterWorkflowFormViews";

function BrandImageSlot({
  label,
  state,
  uploading,
  progress,
  onPick,
  onRemove,
}: {
  label: string;
  state: BrandImageState | null;
  uploading: boolean;
  progress: number;
  onPick: (file: File) => void;
  onRemove: () => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  return (
    <div className="grid gap-2">
      <span className="type-caption text-[var(--fg-2)]">
        {label}
      </span>
      <div className="relative">
        {state ? (
          <div className="relative aspect-square w-full overflow-hidden border border-[var(--border)] bg-[var(--bg-2)]">
            <Image
              src={state.url}
              alt={state.filename}
              fill
              sizes="200px"
              unoptimized
              className="h-full w-full object-cover"
            />
            <button
              type="button"
              onClick={onRemove}
              className="absolute right-2 top-2 inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-full bg-[var(--media-control-bg)] text-[var(--media-control-fg)] backdrop-blur hover:bg-danger hover:text-[var(--media-control-fg)] md:h-7 md:w-7"
              aria-label="移除"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={uploading}
            className="flex aspect-square w-full cursor-pointer flex-col items-center justify-center gap-2 border border-dashed border-[var(--border-strong)] px-3 text-center transition-colors hover:border-accent-border hover:bg-[var(--bg-2)] disabled:opacity-50"
          >
            {uploading ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin text-[var(--fg-1)]" />
                <span className="type-caption tabular-nums text-[var(--fg-1)]">
                  {Math.round(progress * 100).toString().padStart(2, "0")}%
                </span>
              </>
            ) : (
              <>
                <Upload className="h-5 w-5 text-[var(--fg-2)]" />
                <span className="type-caption text-[var(--fg-2)]">
                  点击上传
                </span>
              </>
            )}
          </button>
        )}
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT.join(",")}
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) onPick(file);
          }}
        />
      </div>
      {state ? (
        <p className="truncate type-caption text-[var(--fg-2)]" title={state.filename}>
          {state.filename}
        </p>
      ) : null}
    </div>
  );
}
