"use client";

// Lumen V1.0 登录页（朋友内测）。
// - 左右分栏：左品牌介绍（桌面），右表单卡片（始终显示）
// - 保留现有 401/404/422/429 细分文案
// - 打开密码可见性（Eye / EyeOff）
// - next 参数同源白名单保留

import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { motion } from "framer-motion";
import {
  AlertCircle,
  ArrowRight,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
  Sparkles,
  Wand2,
  Zap,
} from "lucide-react";

import { ApiError, login } from "@/lib/apiClient";
import { errorToText } from "@/lib/errors";

export default function LoginPage() {
  return (
    <Suspense>
      <LoginInner />
    </Suspense>
  );
}

function LoginInner() {
  const router = useRouter();
  const params = useSearchParams();
  const rawNext = params.get("next") || "/";
  const next = safeNextPath(rawNext);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setError("请输入邮箱");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmedEmail)) {
      setError("邮箱格式不正确");
      return;
    }
    if (!password) {
      setError("请输入密码");
      return;
    }

    setSubmitting(true);
    try {
      await login(trimmedEmail, password);
      router.push(next);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401 || err.status === 403) {
          setError("邮箱或密码不正确");
        } else if (err.status === 404) {
          setError("账号不存在");
        } else if (err.status === 422) {
          setError(err.message || "提交内容不合法");
        } else if (err.status === 429) {
          setError("尝试次数过多，请稍后再试");
        } else {
          // 兜底使用统一错误映射，避免暴露原始 ApiError code
          setError(errorToText(err));
        }
      } else {
        setError(errorToText(err));
      }
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200 flex flex-col">
      <main className="flex-1 grid grid-cols-1 md:grid-cols-2 min-h-0">
        {/* —— 左：品牌区（仅桌面） —— */}
        <BrandPanel />

        {/* —— 右：登录表单 —— */}
        <section className="flex items-center justify-center px-4 py-10 md:py-16 safe-x">
          <motion.div
            initial={false}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, ease: "easeOut" }}
            className="w-full max-w-md"
          >
            {/* 移动端品牌头 */}
            <header className="mb-8 md:mb-10 flex items-center gap-3 md:hidden">
              <span className="w-8 h-8 rounded-full bg-gradient-to-tr from-[var(--color-lumen-amber)] to-orange-200 shadow-[0_0_20px_-4px_var(--color-lumen-amber)]" />
              <span className="text-lg font-medium tracking-tight">Lumen</span>
            </header>

            <div className="space-y-6">
              <div>
                <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">
                  欢迎回来
                </h1>
                <p className="text-sm text-[var(--fg-1)] mt-1.5">
                  使用你的 Lumen 账号继续创作。
                </p>
              </div>

              <form onSubmit={onSubmit} className="space-y-4" noValidate>
                <Field id="login-email" label="邮箱" icon={<Mail className="w-3.5 h-3.5" />}>
                  <input
                    id="login-email"
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    autoComplete="email"
                    className="w-full h-10 px-3 rounded-xl bg-[var(--bg-1)]/60 border border-white/10 text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 placeholder:text-neutral-600 transition-colors"
                  />
                </Field>

                <Field
                  id="login-password"
                  label="密码"
                  icon={<Lock className="w-3.5 h-3.5" />}
                >
                  <div className="relative">
                    <input
                      id="login-password"
                      type={showPwd ? "text" : "password"}
                      required
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="输入密码"
                      autoComplete="current-password"
                      className="w-full h-10 pl-3 pr-11 rounded-xl bg-[var(--bg-1)]/60 border border-white/10 text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 placeholder:text-neutral-600 transition-colors"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPwd((v) => !v)}
                      aria-label={showPwd ? "隐藏密码" : "显示密码"}
                      className="absolute right-1 top-1/2 -translate-y-1/2 w-10 h-10 md:w-8 md:h-8 rounded-lg text-neutral-400 hover:text-neutral-100 hover:bg-white/5 flex items-center justify-center transition-colors"
                    >
                      {showPwd ? (
                        <EyeOff className="w-4 h-4" />
                      ) : (
                        <Eye className="w-4 h-4" />
                      )}
                    </button>
                  </div>
                  <div className="mt-1.5 flex justify-end">
                    <Link
                      href={
                        email.trim()
                          ? `/reset-password?email=${encodeURIComponent(email.trim())}`
                          : "/reset-password"
                      }
                      className="text-xs text-[var(--color-lumen-amber)] hover:underline"
                    >
                      忘记密码？
                    </Link>
                  </div>
                </Field>

                {error && (
                  <motion.div
                    initial={{ opacity: 0, y: -2 }}
                    animate={{ opacity: 1, y: 0 }}
                    role="alert"
                    aria-live="assertive"
                    className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300"
                  >
                    <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                    {error}
                  </motion.div>
                )}

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full inline-flex items-center justify-center gap-1.5 h-11 sm:h-10 px-5 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.98] text-black text-sm font-medium disabled:opacity-50 transition-all shadow-[0_8px_24px_-12px_var(--color-lumen-amber)]"
                >
                  {submitting ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      登录中
                    </>
                  ) : (
                    <>
                      登录 <ArrowRight className="w-4 h-4" />
                    </>
                  )}
                </button>
              </form>

              <div className="relative py-1">
                <div className="absolute inset-0 flex items-center">
                  <div className="w-full h-px bg-white/8" />
                </div>
                <div className="relative flex justify-center">
                  <span className="px-3 bg-[var(--bg-0)] text-[11px] uppercase tracking-wider text-neutral-500">
                    没有账号?
                  </span>
                </div>
              </div>

              <p className="text-xs text-neutral-500 text-center leading-relaxed">
                请联系管理员获取邀请链接，或从你收到的{" "}
                <span className="text-neutral-400">/invite/*</span>{" "}
                链接完成注册。
              </p>
            </div>
          </motion.div>
        </section>
      </main>

      <footer className="py-4 px-4 text-center text-xs text-neutral-500 safe-bottom">
        <Link href="/" className="hover:text-neutral-300 transition-colors">
          返回首页
        </Link>
      </footer>
    </div>
  );
}

function safeNextPath(raw: string): string {
  // 严格白名单：只允许相对路径或当前 origin 的 http(s) URL。
  // 杜绝 javascript:/data:/file: + //evil.com/ 类绕过。
  const trimmed = typeof raw === "string" ? raw.trim() : "";
  if (!trimmed) return "/";
  if (trimmed.startsWith("//")) return "/";
  try {
    const base =
      typeof window !== "undefined"
        ? window.location.origin
        : "http://localhost";
    const parsed = new URL(trimmed, base);
    if (parsed.origin !== base) return "/";
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "/";
    if (!parsed.pathname.startsWith("/")) return "/";
    // 显式禁止 javascript: 等被某些浏览器宽松解析的边界
    if (/^javascript:/i.test(trimmed)) return "/";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/";
  }
}

function Field({
  id,
  label,
  icon,
  children,
}: {
  id: string;
  label: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label
        htmlFor={id}
        className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-[var(--fg-1)] mb-1.5"
      >
        {icon}
        {label}
      </label>
      {children}
    </div>
  );
}

function BrandPanel() {
  return (
    <aside className="hidden md:flex relative overflow-hidden bg-[var(--bg-1)]/30 border-r border-white/8">
      {/* 背景装饰 */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute -top-20 -left-20 w-80 h-80 rounded-full bg-[var(--color-lumen-amber)]/10 blur-3xl" />
        <div className="absolute bottom-0 right-0 w-96 h-96 rounded-full bg-orange-500/5 blur-3xl" />
        <div
          className="absolute inset-0 opacity-[0.04]"
          style={{
            backgroundImage:
              "linear-gradient(rgba(255,255,255,0.4) 1px, transparent 1px), linear-gradient(to right, rgba(255,255,255,0.4) 1px, transparent 1px)",
            backgroundSize: "40px 40px",
          }}
        />
      </div>

      <div className="relative z-10 flex flex-col justify-between w-full p-10 lg:p-14">
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3 }}
          className="flex items-center gap-3"
        >
          <span className="w-9 h-9 rounded-full bg-gradient-to-tr from-[var(--color-lumen-amber)] to-orange-200 shadow-[0_0_24px_-4px_var(--color-lumen-amber)]" />
          <span className="text-xl font-semibold tracking-tight">Lumen</span>
        </motion.div>

        <div className="space-y-8 max-w-md">
          <motion.h2
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.35, delay: 0.05 }}
            className="text-3xl lg:text-4xl font-semibold tracking-tight leading-tight"
          >
            让你的灵感
            <br />
            成为画面。
          </motion.h2>

          <ul className="space-y-4">
            <Feature
              icon={<Sparkles className="w-4 h-4" />}
              title="直觉的 prompt 体验"
              desc="自然语言、参考图、风格开关，一气呵成。"
              delay={0.1}
            />
            <Feature
              icon={<Wand2 className="w-4 h-4" />}
              title="对话式迭代"
              desc="边聊边改图，每一次都留在同一会话里。"
              delay={0.15}
            />
            <Feature
              icon={<Zap className="w-4 h-4" />}
              title="实时反馈"
              desc="生成进度、任务状态都在眼前。"
              delay={0.2}
            />
          </ul>
        </div>

        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.3, delay: 0.3 }}
          className="text-xs text-neutral-500"
        >
          V1 内测版 · 朋友专用
        </motion.p>
      </div>
    </aside>
  );
}

function Feature({
  icon,
  title,
  desc,
  delay,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  delay: number;
}) {
  return (
    <motion.li
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay }}
      className="flex items-start gap-3"
    >
      <span className="shrink-0 w-8 h-8 rounded-lg bg-[var(--color-lumen-amber)]/12 border border-[var(--color-lumen-amber)]/25 text-[var(--color-lumen-amber)] flex items-center justify-center">
        {icon}
      </span>
      <div>
        <p className="text-sm text-neutral-100 font-medium">{title}</p>
        <p className="text-xs text-neutral-500 mt-0.5">{desc}</p>
      </div>
    </motion.li>
  );
}
