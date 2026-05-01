"use client";

import { use, useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Lock,
} from "lucide-react";

import { ApiError, apiFetch } from "@/lib/apiClient";
import { errorToText } from "@/lib/errors";

export default function ResetPasswordConfirmPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = use(params);
  return <ResetPasswordConfirm token={token} />;
}

function ResetPasswordConfirm({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const strength = useMemo(() => passwordStrength(password), [password]);
  const passwordTooShort = password.length < 8;
  const confirmMismatch = confirm.length > 0 && confirm !== password;
  const canSubmit =
    !submitting && token.length > 0 && !passwordTooShort && confirm === password;

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (passwordTooShort) {
      setError("密码至少 8 位");
      return;
    }
    if (password !== confirm) {
      setError("两次密码输入不一致");
      return;
    }

    setSubmitting(true);
    try {
      await apiFetch<{ ok: boolean }>("/auth/password/reset-confirm", {
        method: "POST",
        body: JSON.stringify({ token, new_password: password }),
      });
      setDone(true);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 400 || err.status === 404 || err.status === 410) {
          setError(err.message || "重置链接已失效或不正确");
        } else if (err.status === 422) {
          setError(err.message || "提交内容不合法");
        } else if (err.status === 429) {
          setError("请求过于频繁，请稍后再试");
        } else {
          setError(errorToText(err));
        }
      } else {
        setError(errorToText(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200 flex flex-col">
      <section className="flex flex-1 items-center justify-center px-4 py-10 safe-x">
        <div className="w-full max-w-md space-y-6">
          <header className="space-y-2">
            <Link
              href="/login"
              className="inline-flex items-center gap-1.5 text-sm text-neutral-400 transition-colors hover:text-neutral-100"
            >
              <ArrowLeft className="h-4 w-4" />
              返回登录
            </Link>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">
                设置新密码
              </h1>
              <p className="mt-1.5 text-sm text-[var(--fg-1)]">
                新密码至少 8 位。
              </p>
            </div>
          </header>

          {done ? (
            <div className="space-y-4">
              <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5 text-sm text-emerald-200">
                <div className="flex items-start gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                  密码已更新，请重新登录。
                </div>
              </div>
              <Link
                href="/login"
                className="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-xl bg-[var(--color-lumen-amber)] px-5 text-sm font-medium text-black transition-all hover:brightness-110 active:scale-[0.98] sm:h-10"
              >
                去登录 <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          ) : (
            <form onSubmit={onSubmit} className="space-y-4" noValidate>
              <Field id="reset-password" label="新密码" icon={<Lock className="h-3.5 w-3.5" />}>
                <div className="relative">
                  <input
                    id="reset-password"
                    type={showPwd ? "text" : "password"}
                    required
                    minLength={8}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="至少 8 位"
                    autoComplete="new-password"
                    className="h-10 w-full rounded-xl border border-white/10 bg-[var(--bg-1)]/60 pl-3 pr-11 text-base text-neutral-100 transition-colors placeholder:text-neutral-600 focus:border-[var(--color-lumen-amber)]/50 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 md:text-sm"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPwd((value) => !value)}
                    aria-label={showPwd ? "隐藏密码" : "显示密码"}
                    className="absolute right-1 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-lg text-neutral-400 transition-colors hover:bg-white/5 hover:text-neutral-100 md:h-8 md:w-8"
                  >
                    {showPwd ? (
                      <EyeOff className="h-4 w-4" />
                    ) : (
                      <Eye className="h-4 w-4" />
                    )}
                  </button>
                </div>
                <PasswordStrength strength={strength} show={password.length > 0} />
                <p
                  className={
                    "mt-1.5 text-[11px] " +
                    (password.length > 0 && passwordTooShort
                      ? "text-red-300"
                      : "text-neutral-500")
                  }
                >
                  至少 8 位（{Math.min(password.length, 8)}/8）
                </p>
              </Field>

              <Field
                id="reset-confirm"
                label="确认密码"
                icon={<KeyRound className="h-3.5 w-3.5" />}
              >
                <input
                  id="reset-confirm"
                  type={showPwd ? "text" : "password"}
                  required
                  minLength={8}
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  placeholder="再输入一次"
                  autoComplete="new-password"
                  className={
                    "h-10 w-full rounded-xl border bg-[var(--bg-1)]/60 px-3 text-base text-neutral-100 transition-colors placeholder:text-neutral-600 focus:outline-none focus:ring-2 md:text-sm " +
                    (confirmMismatch
                      ? "border-red-500/40 focus:border-red-500/60 focus:ring-red-500/20"
                      : "border-white/10 focus:border-[var(--color-lumen-amber)]/50 focus:ring-[var(--color-lumen-amber)]/25")
                  }
                />
                {confirmMismatch && (
                  <p className="mt-1.5 flex items-center gap-1 text-xs text-red-300">
                    <AlertCircle className="h-3 w-3" /> 两次输入不一致
                  </p>
                )}
              </Field>

              {error && (
                <div className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  {error}
                </div>
              )}

              <button
                type="submit"
                disabled={!canSubmit}
                className="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-xl bg-[var(--color-lumen-amber)] px-5 text-sm font-medium text-black shadow-[0_8px_24px_-12px_var(--color-lumen-amber)] transition-all hover:brightness-110 active:scale-[0.98] disabled:opacity-50 sm:h-10"
              >
                {submitting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    更新中
                  </>
                ) : (
                  <>
                    更新密码 <ArrowRight className="h-4 w-4" />
                  </>
                )}
              </button>
            </form>
          )}
        </div>
      </section>
    </main>
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
    <div>
      <label
        htmlFor={id}
        className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-[var(--fg-1)]"
      >
        {icon}
        {label}
      </label>
      {children}
    </div>
  );
}

type Strength = { score: 0 | 1 | 2 | 3 | 4; label: string; color: string };
const PASSWORD_STRENGTH_SEGMENTS = [
  "length",
  "longer",
  "mixed-case",
  "number-symbol",
] as const;

function passwordStrength(pw: string): Strength {
  let score = 0;
  if (pw.length >= 8) score++;
  if (pw.length >= 12) score++;
  if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) score++;
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) score++;
  const clamped = Math.min(4, score) as 0 | 1 | 2 | 3 | 4;
  const labels = ["太弱", "较弱", "一般", "良好", "强"];
  const colors = [
    "bg-red-500/70",
    "bg-orange-400/70",
    "bg-amber-400/70",
    "bg-emerald-400/70",
    "bg-emerald-400",
  ];
  return { score: clamped, label: labels[clamped], color: colors[clamped] };
}

function PasswordStrength({
  strength,
  show,
}: {
  strength: Strength;
  show: boolean;
}) {
  if (!show) return null;
  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex gap-1">
        {PASSWORD_STRENGTH_SEGMENTS.map((segment, index) => (
          <div
            key={segment}
            className={
              "h-1 flex-1 rounded-full transition-colors duration-200 " +
              (index < strength.score ? strength.color : "bg-white/8")
            }
          />
        ))}
      </div>
      <p className="text-[11px] text-neutral-500">
        强度：<span className="text-neutral-300">{strength.label}</span>
      </p>
    </div>
  );
}
