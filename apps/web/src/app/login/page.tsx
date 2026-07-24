"use client";

// Lumen 登录页。

import { Suspense, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertCircle,
  ArrowRight,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
} from "lucide-react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import {
  ApiError,
  listPublicApiSuppliers,
  login,
  safeAuthNextPath,
} from "@/lib/apiClient";
import { isValidEmailInput, normalizeEmailInput } from "@/lib/email";
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
  const next = safeAuthNextPath(rawNext);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitGuardRef = useRef(false);

  // review §9 / #34: 仅当后端 /auth/api-suppliers 返回非空（即 BYOK 公开注册开启
  // 且至少有一个 public_signup_enabled 的供应商）才展示"直接注册"入口，避免
  // 关闭 BYOK 注册时把用户导向 /signup 然后看见空选择器。
  const byokSuppliersQ = useQuery({
    queryKey: ["auth", "api-suppliers"],
    queryFn: listPublicApiSuppliers,
    retry: false,
    staleTime: 60_000,
  });
  const byokSignupAvailable = (byokSuppliersQ.data?.items?.length ?? 0) > 0;

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const trimmedEmail = normalizeEmailInput(email);
    if (!trimmedEmail) {
      setError("邮箱未填");
      return;
    }
    if (!isValidEmailInput(trimmedEmail)) {
      setError("邮箱格式不正确");
      return;
    }
    if (!password) {
      setError("密码未填");
      return;
    }

    if (submitGuardRef.current) return;
    submitGuardRef.current = true;
    setSubmitting(true);
    try {
      await login(trimmedEmail, password);
      router.replace(next);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "secure_cookie_requires_https") {
          setError(
            "密码验证成功，但当前使用 HTTP，浏览器无法保存 Secure 会话 Cookie。请改用 HTTPS 地址后重新登录。",
          );
        } else if (err.code === "session_unverified") {
          setError("密码验证成功，但登录会话未能确认。请检查 Cookie 或反向代理配置后重试。");
        } else if (
          err.status === 401 ||
          err.status === 403 ||
          err.status === 404
        ) {
          setError("邮箱或密码不正确");
        } else if (err.status === 422) {
          setError("提交内容不合法");
        } else if (err.status === 429) {
          setError("尝试次数过多，请稍后再试");
        } else {
          // 兜底使用统一错误映射，避免暴露原始 ApiError code
          setError(errorToText(err));
        }
      } else {
        setError(errorToText(err));
      }
      submitGuardRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <div className="page-shell">
      <main className="page-scroll flex">
        <section className="auth-stage">
          <motion.div
            initial={false}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            className="auth-frame"
          >
            <header className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                <LumenMark />
              </span>
              <div>
                <p className="type-card-title">Lumen</p>
                <p className="type-caption">创作工作台</p>
              </div>
            </header>

            <div className="grid gap-6">
              <div className="auth-header">
                <h1 className="type-page-title">
                  登录 Lumen
                </h1>
                <p className="type-body mt-1.5">
                  继续你的对话和图片。
                </p>
              </div>

              <form onSubmit={onSubmit} className="auth-form" noValidate>
                <Field id="login-email" label="邮箱" icon={<Mail className="w-3.5 h-3.5" />}>
                  <input
                    id="login-email"
                    name="email"
                    type="email"
                    required
                    disabled={submitting}
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="name@示例.com"
                    autoComplete="email"
                    inputMode="email"
                    autoCapitalize="none"
                    autoCorrect="off"
                    enterKeyHint="next"
                    className="auth-control px-3"
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
                      name="password"
                      type={showPwd ? "text" : "password"}
                      required
                      disabled={submitting}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="输入密码"
                      autoComplete="current-password"
                      enterKeyHint="go"
                      className="auth-control pl-3 pr-12"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPwd((v) => !v)}
                      disabled={submitting}
                      aria-label={showPwd ? "隐藏密码" : "显示密码"}
                      className="absolute right-0 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-[var(--radius-card)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:opacity-50"
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
                          ? `/reset-password?email=${encodeURIComponent(
                              normalizeEmailInput(email),
                            )}`
                          : "/reset-password"
                      }
                      className="type-caption text-[var(--accent)] hover:underline"
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
                    className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger"
                  >
                    <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                    {error}
                  </motion.div>
                )}

                <button
                  type="submit"
                  disabled={submitting}
                  aria-busy={submitting}
                  className="type-control inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-5 text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,background-color,opacity] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)] disabled:opacity-50"
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
                  <div className="w-full h-px bg-[var(--border)]" />
                </div>
                <div className="relative flex justify-center">
                  <span className="type-caption bg-[var(--bg-0)] px-3">
                    还没有账号?
                  </span>
                </div>
              </div>

              <div className="type-caption space-y-1 text-center">
                {byokSignupAvailable && (
                  <p>
                    有 API Key？{" "}
                    <Link href="/signup" className="text-[var(--accent)] hover:underline">
                      直接注册
                    </Link>
                  </p>
                )}
                <p>
                  也可以打开收到的 <span className="text-[var(--fg-1)]">/invite/*</span> 链接注册。
                </p>
              </div>
            </div>
          </motion.div>
        </section>
      </main>

      <footer className="px-4 pb-[calc(1.5rem+env(safe-area-inset-bottom,0px))] pt-4 text-center text-xs text-[var(--fg-2)]">
        <Link href="/" className="inline-flex min-h-11 items-center justify-center px-2 hover:text-[var(--fg-0)] transition-colors">
          返回首页
        </Link>
      </footer>
    </div>
  );
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
    <div className="auth-field">
      <label
        htmlFor={id}
        className="type-label flex items-center gap-1.5"
      >
        {icon}
        {label}
      </label>
      {children}
    </div>
  );
}
