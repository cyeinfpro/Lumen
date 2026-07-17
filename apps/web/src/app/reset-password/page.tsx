"use client";

import { Suspense, useRef, useState } from "react";
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
import { isValidEmailInput, normalizeEmailInput } from "@/lib/email";
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
  const submitGuardRef = useRef(false);

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSentTo(null);

    const trimmedEmail = normalizeEmailInput(email);
    if (!trimmedEmail) {
      setError("邮箱未填");
      return;
    }
    if (!isValidEmailInput(trimmedEmail)) {
      setError("邮箱格式不正确");
      return;
    }

    if (submitGuardRef.current) return;
    submitGuardRef.current = true;
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
      submitGuardRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <main className="page-shell">
      <section className="auth-stage">
        <div className="auth-frame">
          <header className="auth-header">
            <Link
              href="/login"
              className="type-body-sm inline-flex items-center gap-1.5 transition-colors hover:text-[var(--fg-0)]"
            >
              <ArrowLeft className="h-4 w-4" />
              返回登录
            </Link>
            <div>
              <h1 className="type-page-title">
                重置密码
              </h1>
              <p className="type-body mt-1.5">
                输入账号邮箱，获取密码重置链接。
              </p>
            </div>
          </header>

          <form onSubmit={onSubmit} className="auth-form" noValidate>
            <Field id="reset-email" label="邮箱" icon={<Mail className="h-3.5 w-3.5" />}>
              <input
                id="reset-email"
                name="email"
                type="email"
                required
                disabled={submitting}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                inputMode="email"
                autoCapitalize="none"
                autoCorrect="off"
                enterKeyHint="send"
                className="auth-control px-3"
              />
            </Field>

            {sentTo && (
              <div
                role="status"
                aria-live="polite"
                className="flex items-start gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-3 py-2 type-body-sm text-success"
              >
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                如果该邮箱存在，重置链接会发送到 {sentTo}。
              </div>
            )}

            {error && (
              <div
                role="alert"
                aria-live="assertive"
                className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={submitting}
              aria-busy={submitting}
              className="type-control inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-5 text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,filter,opacity] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)] disabled:opacity-50"
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
