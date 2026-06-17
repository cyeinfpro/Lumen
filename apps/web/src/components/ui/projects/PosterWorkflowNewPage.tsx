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
  ArrowRight,
  ChevronDown,
  Loader2,
  Palette,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import { useCreatePosterDesignWorkflowMutation } from "@/lib/queries";
import {
  API_BASE,
  type PosterAspectRatio,
  type PosterStyleItem,
} from "@/lib/apiClient";
import { ensureCsrfToken, refreshCsrfToken } from "@/lib/api/http";
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
      if (csrf) xhr.setRequestHeader("x-csrf-token", csrf);

      const fd = new FormData();
      fd.append("file", file);

      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as UploadResult);
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

interface BrandImageState {
  url: string;
  id: string;
  filename: string;
  size: number;
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
  const [uploading, setUploading] = useState<null | "logo" | "product">(null);
  const [uploadProgress, setUploadProgress] = useState(0);

  const logoUrlRef = useRef<string | null>(null);
  const productUrlRef = useRef<string | null>(null);
  useEffect(() => {
    return () => {
      if (logoUrlRef.current) URL.revokeObjectURL(logoUrlRef.current);
      if (productUrlRef.current) URL.revokeObjectURL(productUrlRef.current);
    };
  }, []);

  const create = useCreatePosterDesignWorkflowMutation({
    onError: (err) =>
      toast.error("创建项目失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: (out) => {
      toast.success("项目已创建");
      router.push(`/projects/${out.workflow_run_id}`);
    },
  });

  const copyTrimmed = copy.trim();
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
    async (kind: "logo" | "product", file: File) => {
      if (!ACCEPT.includes(file.type)) {
        toast.error(`不支持的格式：${file.type || "未知"}`);
        return;
      }
      if (file.size > MAX_BRAND_IMAGE_BYTES) {
        toast.error(`单张不能超过 ${formatBytes(MAX_BRAND_IMAGE_BYTES)}`);
        return;
      }
      const localUrl = URL.createObjectURL(file);
      if (kind === "logo") {
        if (logoUrlRef.current) URL.revokeObjectURL(logoUrlRef.current);
        logoUrlRef.current = localUrl;
      } else {
        if (productUrlRef.current) URL.revokeObjectURL(productUrlRef.current);
        productUrlRef.current = localUrl;
      }
      setUploading(kind);
      setUploadProgress(0);
      const controller = new AbortController();
      try {
        const out = await uploadWithProgress(
          file,
          (ratio) => setUploadProgress(ratio),
          controller.signal,
        );
        const value: BrandImageState = {
          url: localUrl,
          id: out.id,
          filename: file.name,
          size: file.size,
        };
        if (kind === "logo") setLogo(value);
        else setProduct(value);
      } catch (err) {
        toast.error("上传失败", {
          description: err instanceof Error ? err.message : "请稍后重试",
        });
      } finally {
        setUploading(null);
        setUploadProgress(0);
      }
    },
    [],
  );

  const removeBrandImage = (kind: "logo" | "product") => {
    if (kind === "logo") {
      if (logoUrlRef.current) {
        URL.revokeObjectURL(logoUrlRef.current);
        logoUrlRef.current = null;
      }
      setLogo(null);
    } else {
      if (productUrlRef.current) {
        URL.revokeObjectURL(productUrlRef.current);
        productUrlRef.current = null;
      }
      setProduct(null);
    }
  };

  const derivedTitle = useMemo(() => {
    if (title.trim()) return title.trim();
    if (!copyTrimmed) return "海报设计";
    return copyTrimmed.split(/\n/)[0]?.slice(0, 24) || "海报设计";
  }, [title, copyTrimmed]);

  const ctaDisabled =
    !copyTrimmed ||
    !style ||
    !aspects.length ||
    create.isPending ||
    uploading !== null;

  const onCreate = () => {
    setError(null);
    if (!copyTrimmed) {
      setError("请输入海报文案");
      return;
    }
    if (!style) {
      setError("请选择海报风格");
      return;
    }
    if (!aspects.length) {
      setError("至少选择一个目标尺寸");
      return;
    }
    create.mutate({
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
  };

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="新建"
        subtitle="新建海报项目"
        backHref="/projects"
        backLabel="返回项目"
      />
      <ProjectTopBar />

      <main className="lumen-studio-bg project-mobile-scroll-with-cta mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pt-2 md:mb-0 md:px-6 md:pb-8 md:pt-3">
        <div className="mx-auto grid w-full max-w-[1280px] gap-3">
          <header className="hidden min-w-0 items-center justify-between gap-3 border-b border-[var(--border)] pb-1.5 md:flex">
            <div className="flex min-w-0 items-baseline gap-2.5">
              <p className="type-page-kicker shrink-0">新建项目</p>
              <h1 className="type-page-title shrink-0">新建海报设计</h1>
              <p className="type-page-subtitle hidden min-w-0 truncate lg:block">
                录入文案、选择风格、确定尺寸；剩下交给 AI。
              </p>
            </div>
            <Link
              href="/projects"
              className="inline-flex min-h-9 shrink-0 items-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              返回项目
            </Link>
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
                className="-mt-3 w-full resize-y border-b border-[var(--border)] bg-transparent px-1 py-2 text-[15px] leading-7 text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
              />

              {/* Style */}
              <SectionHeader
                eyebrow="N°02 — 风格"
                title="海报风格"
                trailing={
                  <button
                    type="button"
                    onClick={() => setStyleOpen(true)}
                    className="inline-flex min-h-9 items-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-amber)] hover:text-[var(--amber-300)]"
                  >
                    <Palette className="h-3.5 w-3.5" />
                    {style ? "更换风格" : "从风格库选择"}
                  </button>
                }
              />
              <div className="-mt-2">
                {style ? (
                  <StyleSummary style={style} onClear={() => setStyle(null)} />
                ) : (
                  <button
                    type="button"
                    onClick={() => setStyleOpen(true)}
                    className="flex w-full min-h-[120px] flex-col items-center justify-center gap-2 border border-dashed border-[var(--border-strong)] px-3 text-center transition-colors hover:border-[var(--border-amber)] hover:bg-[var(--accent-soft)]"
                  >
                    <Palette className="h-5 w-5 text-[var(--fg-2)]" />
                    <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-1)]">
                      点击选择风格
                    </p>
                    <p className="text-[12px] text-[var(--fg-3)]">
                      没有合适的风格？可去「风格库」创建。
                    </p>
                  </button>
                )}
              </div>

              {/* Aspects */}
              <SectionHeader
                eyebrow="N°03 — 尺寸"
                title="目标尺寸"
                trailing={
                  <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
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
                        "inline-flex min-h-9 cursor-pointer items-center rounded-full border px-3 text-[12px] transition-colors",
                        active
                          ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
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
                    <h2 className="type-section-title mt-2 md:text-[22px]">
                      品牌资产
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
                    uploading={uploading === "logo"}
                    progress={uploadProgress}
                    onPick={(file) => onPickBrandImage("logo", file)}
                    onRemove={() => removeBrandImage("logo")}
                  />
                  <BrandImageSlot
                    label="产品图"
                    state={product}
                    uploading={uploading === "product"}
                    progress={uploadProgress}
                    onPick={(file) => onPickBrandImage("product", file)}
                    onRemove={() => removeBrandImage("product")}
                  />
                </div>

                <div className="mt-5 grid gap-x-8 gap-y-5 md:grid-cols-2">
                  <label className="block min-w-0">
                    <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                      主色
                    </span>
                    <div className="mt-2 flex min-w-0 items-center gap-3">
                      <input
                        type="color"
                        value={primaryColor || "#ffd166"}
                        onChange={(event) => setPrimaryColor(event.target.value)}
                        className="h-9 w-12 cursor-pointer border border-[var(--border)] bg-transparent"
                      />
                      <input
                        value={primaryColor}
                        onChange={(event) =>
                          setPrimaryColor(event.target.value.slice(0, 24))
                        }
                        maxLength={24}
                        placeholder="#FFD166 / amber"
                        className="h-10 min-w-0 flex-1 border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                      />
                    </div>
                  </label>
                  <label className="block min-w-0">
                    <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                      字体
                    </span>
                    <input
                      value={fontFamily}
                      onChange={(event) =>
                        setFontFamily(event.target.value.slice(0, 64))
                      }
                      maxLength={64}
                      placeholder="例如：思源黑体 / Inter"
                      className="mt-2 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                    />
                  </label>
                </div>
              </details>

              {/* Quality + title */}
              <SectionHeader eyebrow="N°05 — 设置" title="项目设置" />
              <div className="-mt-2 grid gap-5 md:grid-cols-2">
                <label className="block min-w-0">
                  <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                    标题
                  </span>
                  <input
                    value={title}
                    onChange={(event) =>
                      setTitle(event.target.value.slice(0, TITLE_MAX))
                    }
                    maxLength={TITLE_MAX}
                    placeholder={derivedTitle}
                    className="mt-2 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                  />
                  <CharCount remaining={titleRemaining} max={TITLE_MAX} />
                </label>
                <div>
                  <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
                    质量
                  </span>
                  <div className="mt-2 inline-flex rounded-full border border-[var(--border)] p-0.5">
                    <button
                      type="button"
                      onClick={() => setQualityMode("standard")}
                      className={cn(
                        "inline-flex min-h-9 items-center rounded-full px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                        qualityMode === "standard"
                          ? "bg-[var(--amber-400)] text-black"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                      )}
                    >
                      标准
                    </button>
                    <button
                      type="button"
                      onClick={() => setQualityMode("premium")}
                      className={cn(
                        "inline-flex min-h-9 items-center rounded-full px-3 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                        qualityMode === "premium"
                          ? "bg-[var(--amber-400)] text-black"
                          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                      )}
                    >
                      高质量
                    </button>
                  </div>
                </div>
              </div>

              {error ? (
                <div className="border-y border-[var(--danger)]/30 bg-[var(--danger-soft)]/30 px-4 py-4 md:px-5">
                  <div className="flex items-start gap-3">
                    <X className="mt-0.5 h-4 w-4 shrink-0 text-[var(--danger)]" />
                    <div>
                      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--danger)]">
                        错误
                      </p>
                      <p className="mt-1 text-[13px] text-[var(--fg-0)]">{error}</p>
                    </div>
                  </div>
                </div>
              ) : null}

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
                  {create.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : null}
                  <span>
                    {create.isPending ? "创建中" : "创建海报项目"}
                  </span>
                  {!create.isPending ? (
                    <ArrowRight className="h-4 w-4 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-enabled:group-hover:translate-x-0 group-enabled:group-hover:opacity-100" />
                  ) : null}
                </button>
              </div>
            </section>

            <aside className="hidden grid-cols-1 gap-0 self-start lg:grid">
              <InfoPanel title="流程">
                <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
                  文案切分、4 张母版候选、多尺寸成品（默认 4 尺寸），可逐张返修。
                </p>
              </InfoPanel>
              <InfoPanel title="文字策略">
                <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
                  V1 全 AI 出图（无文字层 Canvas 编辑器）。所有文字直接写在 prompt 里。
                </p>
              </InfoPanel>
              <InfoPanel title="风格">
                <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
                  在「风格库」沉淀常用风格，每次创建项目只挑一个；保证视觉一致性。
                </p>
              </InfoPanel>
            </aside>
          </div>
        </div>
      </main>

      <div className="fixed inset-x-0 bottom-[calc(56px+env(safe-area-inset-bottom,0px))] z-30 border-t border-[var(--border)] bg-[var(--bg-0)]/95 px-4 py-3 backdrop-blur md:hidden">
        <button
          type="button"
          onClick={onCreate}
          disabled={ctaDisabled}
          className={cn(
            "inline-flex w-full items-center justify-center gap-2 rounded-full px-6 py-3.5 text-[15px] font-medium text-black transition-[opacity,transform] duration-[var(--dur-base)]",
            ctaDisabled
              ? "cursor-not-allowed bg-[var(--fg-3)] opacity-60"
              : "cursor-pointer bg-[var(--accent)] shadow-[var(--shadow-amber)] active:scale-[0.98]",
          )}
        >
          {create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
          {create.isPending ? "创建中" : "创建海报项目"}
        </button>
      </div>

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
          <p className="type-page-kicker">{eyebrow}</p>
          <h2 className="type-section-title mt-2 md:text-[22px]">{title}</h2>
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

function StyleSummary({
  style,
  onClear,
}: {
  style: PosterStyleItem;
  onClear: () => void;
}) {
  const coverUrl =
    style.display_url || style.cover_image_url || style.thumb_url || "";
  return (
    <div className="grid min-w-0 grid-cols-[72px_minmax(0,1fr)_auto] gap-3 border-b border-[var(--border)] pb-3 sm:grid-cols-[88px_minmax(0,1fr)_auto]">
      <div className="relative aspect-square overflow-hidden bg-[var(--bg-2)]">
        {coverUrl ? (
          <Image
            src={coverUrl}
            alt={style.title}
            fill
            sizes="88px"
            unoptimized
            className="h-full w-full object-cover"
          />
        ) : null}
      </div>
      <div className="min-w-0">
        <p className="line-clamp-1 text-[14px] font-medium tracking-tight text-[var(--fg-0)]">
          {style.title}
        </p>
        {style.mood ? (
          <p className="mt-1 line-clamp-1 text-[12px] text-[var(--fg-2)]">
            {style.mood}
          </p>
        ) : null}
        {style.style_tags.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {style.style_tags.slice(0, 6).map((tag) => (
              <span
                key={tag}
                className="inline-flex max-w-full items-center rounded-full border border-[var(--border)] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]"
              >
                <span className="truncate">{tag}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onClear}
        aria-label="清除选择"
        className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

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
      <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
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
              className="absolute right-2 top-2 inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-full bg-black/55 text-white/85 backdrop-blur hover:bg-[var(--danger)]/70 hover:text-white"
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
            className="flex aspect-square w-full cursor-pointer flex-col items-center justify-center gap-2 border border-dashed border-[var(--border-strong)] px-3 text-center transition-colors hover:border-[var(--border-amber)] hover:bg-white/[0.02] disabled:opacity-50"
          >
            {uploading ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin text-[var(--fg-1)]" />
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] tabular-nums text-[var(--fg-1)]">
                  {Math.round(progress * 100).toString().padStart(2, "0")}%
                </span>
              </>
            ) : (
              <>
                <Upload className="h-5 w-5 text-[var(--fg-2)]" />
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
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
        <p className="truncate text-[11px] text-[var(--fg-2)]" title={state.filename}>
          {state.filename}
        </p>
      ) : null}
    </div>
  );
}
