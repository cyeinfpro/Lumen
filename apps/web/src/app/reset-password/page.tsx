"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Loader2,
  Mail,
  Send,
} from "lucide-react";

import { ApiError, apiFetch } from "@/lib/apiClient";
import { errorToText } from "@/lib/errors";

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <ResetPasswordInner />
    </Suspense>
  );
}

function ResetPasswordInner() {
  const params = useSearchParams();
  const [email, setEmail] = useState(() => params.get("email") ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSentTo(null);

    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setError("请输入邮箱");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmedEmail)) {
      setError("邮箱格式不正确");
      return;
    }

    setSubmitting(true);
    try {
      await apiFetch<{ ok: boolean }>("/auth/password/reset-request", {
        method: "POST",
        body: JSON.stringify({ email: trimmedEmail }),
      });
      setSentTo(trimmedEmail);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError("请求过于频繁，请稍后再试");
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
                重置密码
              </h1>
              <p className="mt-1.5 text-sm text-[var(--fg-1)]">
                输入账号邮箱，获取密码重置链接。
              </p>
            </div>
          </header>

          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <Field id="reset-email" label="邮箱" icon={<Mail className="h-3.5 w-3.5" />}>
              <input
                id="reset-email"
                type="email"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                className="h-10 w-full rounded-xl border border-white/10 bg-[var(--bg-1)]/60 px-3 text-base text-neutral-100 transition-colors placeholder:text-neutral-600 focus:border-[var(--color-lumen-amber)]/50 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 md:text-sm"
              />
            </Field>

            {sentTo && (
              <div className="flex items-start gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                如果该邮箱存在，重置链接会发送到 {sentTo}。
              </div>
            )}

            {error && (
              <div className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={submitting}
              className="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-xl bg-[var(--color-lumen-amber)] px-5 text-sm font-medium text-black shadow-[0_8px_24px_-12px_var(--color-lumen-amber)] transition-all hover:brightness-110 active:scale-[0.98] disabled:opacity-50 sm:h-10"
            >
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  发送中
                </>
              ) : (
                <>
                  发送重置链接 <Send className="h-4 w-4" />
                </>
              )}
            </button>
          </form>
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
