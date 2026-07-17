"use client";

import { use, useDeferredValue, useMemo, useRef, useState } from "react";
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

function canSubmitResetPassword({
  submitting,
  token,
  passwordTooShort,
  passwordsMatch,
}: {
  submitting: boolean;
  token: string;
  passwordTooShort: boolean;
  passwordsMatch: boolean;
}): boolean {
  return !submitting && token.length > 0 && !passwordTooShort && passwordsMatch;
}

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
  const submitGuardRef = useRef(false);

  const deferredPassword = useDeferredValue(password);
  const strength = useMemo(
    () => passwordStrength(deferredPassword),
    [deferredPassword],
  );
  const passwordTooShort = password.length < 8;
  const confirmMismatch = confirm.length > 0 && confirm !== password;
  const canSubmit = canSubmitResetPassword({
    submitting,
    token,
    passwordTooShort,
    passwordsMatch: confirm === password,
  });

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

    if (submitGuardRef.current) return;
    submitGuardRef.current = true;
    setSubmitting(true);
    try {
      await apiFetch<{ ok: boolean }>("/auth/password/reset-confirm", {
        method: "POST",
        body: JSON.stringify({ token, new_password: password }),
      });
      setPassword("");
      setConfirm("");
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
                设置新密码
              </h1>
              <p className="type-body mt-1.5">
                新密码至少 8 位。
              </p>
            </div>
          </header>

          {done ? (
            <div className="space-y-4">
              <div
                role="status"
                aria-live="polite"
                className="rounded-[var(--radius-dialog)] border border-success-border bg-success-soft p-5 type-body-sm text-success"
              >
                <div className="flex items-start gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                  密码已更新，请重新登录。
                </div>
              </div>
              <Link
                href="/login"
                replace
                className="type-control inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-5 text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,background-color] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)]"
              >
                去登录 <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          ) : (
            <form onSubmit={onSubmit} className="auth-form" noValidate>
              <Field id="reset-password" label="新密码" icon={<Lock className="h-3.5 w-3.5" />}>
                <div className="relative">
                  <input
                    id="reset-password"
                    name="password"
                    type={showPwd ? "text" : "password"}
                    required
                    disabled={submitting}
                    minLength={8}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="至少 8 位"
                    autoComplete="new-password"
                    enterKeyHint="next"
                    className="auth-control pl-3 pr-12"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPwd((value) => !value)}
                    disabled={submitting}
                    aria-label={showPwd ? "隐藏密码" : "显示密码"}
                    className="absolute right-0 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-[var(--radius-card)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:opacity-50"
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
                      ? "text-danger"
                      : "text-[var(--fg-2)]")
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
                  name="password-confirmation"
                  type={showPwd ? "text" : "password"}
                  required
                  disabled={submitting}
                  minLength={8}
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  placeholder="再输入一次"
                  autoComplete="new-password"
                  enterKeyHint="done"
                  className={
                    "auth-control px-3 " +
                    (confirmMismatch
                      ? "border-danger-border focus:border-danger focus:ring-danger/20"
                      : "border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-[var(--color-lumen-amber)]/25")
                  }
                />
                {confirmMismatch && (
                  <p
                    role="alert"
                    aria-live="assertive"
                    className="mt-1.5 flex items-center gap-1 type-caption text-danger"
                  >
                    <AlertCircle className="h-3 w-3" /> 两次输入不一致
                  </p>
                )}
              </Field>

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
                disabled={!canSubmit}
                aria-busy={submitting}
                className="type-control inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-5 text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,background-color,opacity] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)] disabled:opacity-50"
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
    "bg-danger/70",
    "bg-warning/70",
    "bg-warning/70",
    "bg-success/70",
    "bg-success",
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
              (index < strength.score ? strength.color : "bg-[var(--bg-2)]")
            }
          />
        ))}
      </div>
      <p className="text-[11px] text-[var(--fg-2)]">
        强度：<span className="text-[var(--fg-1)]">{strength.label}</span>
      </p>
    </div>
  );
}
