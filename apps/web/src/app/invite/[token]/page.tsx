"use client";

// 邀请注册页：/invite/{token}
// Next 16 dynamic params 是 Promise —— Client Component 用 React.use() 解包。
// 流程：
//  1. usePublicInviteQuery 拉 invite info（公共，不带 cookie）
//  2. valid=false 显示明确文案（按 invalid_reason）
//  3. valid=true 渲染注册表单：email（如果 invite.email 已绑定则锁定）/ password / 确认密码
//     附带一个 live 密码强度指示器
//  4. 提交 → signup(email, password, token)；成功 router.push("/")；失败 inline 显示

import {
  use,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import { format } from "date-fns";
import {
  AlertCircle,
  ArrowRight,
  Check,
  Clock,
  Eye,
  EyeOff,
  FileX,
  KeyRound,
  Loader2,
  Lock,
  Mail,
  ShieldOff,
  Sparkles,
  UserCog,
  Users as UsersIcon,
} from "lucide-react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import { usePublicInviteQuery } from "@/lib/queries";
import { ApiError, signup } from "@/lib/apiClient";
import { isValidEmailInput, normalizeEmailInput } from "@/lib/email";
import type { InviteLinkPublicOut } from "@/lib/types";

export default function InvitePage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = use(params);
  const q = usePublicInviteQuery(token);
  const showSkeleton = useDelayedFlag(q.isLoading, 180);

  return (
    <div className="page-shell">
      <main className="auth-stage">
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
              <p className="type-caption">邀请注册</p>
            </div>
          </header>

          {q.isLoading ? (
            showSkeleton ? <SkeletonInvite /> : null
          ) : q.isError ? (
            <ErrorView
              error={q.error}
              pending={q.isFetching}
              onRetry={() => void q.refetch()}
            />
          ) : q.data ? (
            q.data.valid ? (
              <SignupForm token={token} invite={q.data} />
            ) : (
              <InvalidView invite={q.data} />
            )
          ) : null}
        </motion.div>
      </main>

      <footer className="px-4 pb-[calc(1.5rem+env(safe-area-inset-bottom,0px))] pt-4 text-center text-xs text-[var(--fg-2)]">
        <Link
          href="/login"
          className="inline-flex min-h-11 items-center justify-center px-2 hover:text-[var(--fg-0)] transition-colors"
        >
          已有账号？直接登录
        </Link>
      </footer>
    </div>
  );
}

function useDelayedFlag(active: boolean, delayMs: number): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(
      () => setVisible(active),
      active ? delayMs : 0,
    );
    return () => window.clearTimeout(timer);
  }, [active, delayMs]);
  return visible;
}

function SkeletonInvite() {
  return (
    <div className="space-y-5">
      <div className="h-8 w-48 bg-[var(--bg-2)] rounded animate-pulse" />
      <div className="h-4 w-72 bg-[var(--bg-2)] rounded animate-pulse" />
      <div className="h-40 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] animate-pulse mt-6" />
      <div className="h-44 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] animate-pulse" />
    </div>
  );
}

function canSubmitInviteSignup({
  submitting,
  normalizedEmail,
  passwordTooShort,
  passwordsMatch,
}: {
  submitting: boolean;
  normalizedEmail: string;
  passwordTooShort: boolean;
  passwordsMatch: boolean;
}): boolean {
  return (
    !submitting &&
    normalizedEmail.length > 0 &&
    isValidEmailInput(normalizedEmail) &&
    !passwordTooShort &&
    passwordsMatch
  );
}

function SignupForm({
  token,
  invite,
}: {
  token: string;
  invite: InviteLinkPublicOut;
}) {
  const router = useRouter();
  const lockedEmail = invite.email != null;

  const [emailInput, setEmailInput] = useState("");
  const email = lockedEmail ? (invite.email as string) : emailInput;
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitGuardRef = useRef(false);

  const expiresLabel = useMemo(() => {
    if (!invite.expires_at) return "永久";
    try {
      return format(new Date(invite.expires_at), "yyyy-MM-dd HH:mm");
    } catch {
      return invite.expires_at;
    }
  }, [invite.expires_at]);

  const normalizedEmail = normalizeEmailInput(email);
  const deferredPassword = useDeferredValue(password);
  const strength = useMemo(
    () => passwordStrength(deferredPassword),
    [deferredPassword],
  );
  const confirmMismatch = confirm.length > 0 && confirm !== password;
  const passwordTooShort = password.length < 8;
  const canSubmit = canSubmitInviteSignup({
    submitting,
    normalizedEmail,
    passwordTooShort,
    passwordsMatch: confirm === password,
  });

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
    if (password.length < 8) {
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
      await signup(trimmedEmail, password, token);
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          setError("该邮箱已注册，请直接登录");
        } else if (err.status === 403) {
          setError("邀请被拒绝（可能与绑定邮箱不一致）");
        } else if (err.status === 410 || err.status === 404) {
          setError("邀请已失效或不存在");
        } else if (err.status === 422) {
          setError("提交内容不合法");
        } else {
          setError("注册失败，请稍后重试");
        }
      } else {
        setError("注册失败，请稍后重试");
      }
      submitGuardRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <div className="grid gap-6">
      <div className="auth-header">
        <h1 className="type-page-title">
          加入 Lumen
        </h1>
        <p className="type-body mt-1.5">
          使用邀请链接创建你的账号。
        </p>
      </div>

      {/* 邀请信息 */}
      <div className="surface-section grid gap-2 py-4">
        <div className="type-label flex items-center gap-1.5 text-[var(--accent)]">
          <Sparkles className="w-3.5 h-3.5" /> 邀请详情
        </div>
        <InfoLine label="角色" icon={<UserCog className="w-3 h-3" />}>
          <RoleBadge role={invite.role} />
        </InfoLine>
        {invite.email && (
          <InfoLine label="绑定邮箱" icon={<Mail className="w-3 h-3" />}>
            <span className="text-[var(--fg-0)]">{invite.email}</span>
          </InfoLine>
        )}
        <InfoLine label="过期" icon={<Clock className="w-3 h-3" />}>
          <span className="text-[var(--fg-0)] font-mono tabular-nums text-xs">
            {expiresLabel}
          </span>
        </InfoLine>
      </div>

      <form onSubmit={onSubmit} className="auth-form">
        <Field id="invite-email" label="邮箱" icon={<Mail className="w-3.5 h-3.5" />}>
          <input
            id="invite-email"
            name="email"
            type="email"
            required
            disabled={submitting}
            readOnly={lockedEmail}
            value={email}
            onChange={(e) => {
              if (lockedEmail) return;
              setEmailInput(e.target.value);
            }}
            placeholder="you@example.com"
            autoComplete="email"
            inputMode="email"
            autoCapitalize="none"
            autoCorrect="off"
            enterKeyHint="next"
            className={
              "auth-control px-3 " +
              (lockedEmail ? "opacity-70 cursor-not-allowed" : "")
            }
          />
          {lockedEmail && (
            <p className="text-[11px] text-[var(--fg-2)] mt-1">
              该邀请已绑定此邮箱，不能修改。
            </p>
          )}
        </Field>

        <InvitePasswordFields
          password={password}
          confirm={confirm}
          showPassword={showPwd}
          submitting={submitting}
          passwordTooShort={passwordTooShort}
          confirmMismatch={confirmMismatch}
          strength={strength}
          onPasswordChange={setPassword}
          onConfirmChange={setConfirm}
          onTogglePassword={() => setShowPwd((value) => !value)}
        />

        <AnimatePresence>
          {error && (
            <motion.div
              initial={{ opacity: 0, y: -2 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              role="alert"
              aria-live="assertive"
              className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger"
            >
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              {error}
            </motion.div>
          )}
        </AnimatePresence>

        <button
          type="submit"
          disabled={!canSubmit}
          aria-busy={submitting}
          className="type-control inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-5 text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,background-color,opacity] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)] disabled:opacity-50"
        >
          {submitting ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin" /> 注册中
            </>
          ) : (
            <>
              创建账号并进入 Lumen <ArrowRight className="w-4 h-4" />
            </>
          )}
        </button>
      </form>

      <p className="text-xs text-[var(--fg-2)] text-center">
        已有账号？{" "}
        <Link
          href="/login"
          className="text-[var(--color-lumen-amber)] hover:underline"
        >
          直接登录
        </Link>
      </p>
    </div>
  );
}

function InvitePasswordFields({
  password,
  confirm,
  showPassword,
  submitting,
  passwordTooShort,
  confirmMismatch,
  strength,
  onPasswordChange,
  onConfirmChange,
  onTogglePassword,
}: {
  password: string;
  confirm: string;
  showPassword: boolean;
  submitting: boolean;
  passwordTooShort: boolean;
  confirmMismatch: boolean;
  strength: Strength;
  onPasswordChange: (value: string) => void;
  onConfirmChange: (value: string) => void;
  onTogglePassword: () => void;
}) {
  const passwordInputType = showPassword ? "text" : "password";
  const passwordToggleLabel = showPassword ? "隐藏密码" : "显示密码";
  const passwordLengthTone =
    password.length > 0 && passwordTooShort
      ? "text-danger"
      : "text-[var(--fg-2)]";
  const confirmInputTone = confirmMismatch
    ? "border-danger-border focus:border-danger focus:ring-danger/20"
    : "border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-[var(--color-lumen-amber)]/25";

  return (
    <>
      <Field
        id="invite-password"
        label="密码"
        icon={<Lock className="w-3.5 h-3.5" />}
      >
        <div className="relative">
          <input
            id="invite-password"
            name="password"
            type={passwordInputType}
            required
            disabled={submitting}
            minLength={8}
            value={password}
            onChange={(event) => onPasswordChange(event.target.value)}
            placeholder="至少 8 位"
            autoComplete="new-password"
            enterKeyHint="next"
            className="auth-control pl-3 pr-12"
          />
          <button
            type="button"
            onClick={onTogglePassword}
            disabled={submitting}
            aria-label={passwordToggleLabel}
            className="absolute right-0 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-[var(--radius-card)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:opacity-50"
          >
            {showPassword ? (
              <EyeOff className="w-4 h-4" />
            ) : (
              <Eye className="w-4 h-4" />
            )}
          </button>
        </div>
        <PasswordStrength strength={strength} show={password.length > 0} />
        <p className={`mt-1.5 text-[11px] ${passwordLengthTone}`}>
          至少 8 位（{Math.min(password.length, 8)}/8）
        </p>
      </Field>

      <Field
        id="invite-confirm"
        label="确认密码"
        icon={<KeyRound className="w-3.5 h-3.5" />}
      >
        <input
          id="invite-confirm"
          name="password-confirmation"
          type={passwordInputType}
          required
          disabled={submitting}
          minLength={8}
          value={confirm}
          onChange={(event) => onConfirmChange(event.target.value)}
          placeholder="再输入一次"
          autoComplete="new-password"
          enterKeyHint="done"
          className={`auth-control px-3 ${confirmInputTone}`}
        />
        {confirmMismatch ? (
          <p
            role="alert"
            aria-live="assertive"
            className="flex items-center gap-1 type-caption text-danger mt-1.5"
          >
            <AlertCircle className="w-3 h-3" /> 两次输入不一致
          </p>
        ) : null}
      </Field>
    </>
  );
}

function InvalidView({ invite }: { invite: InviteLinkPublicOut }) {
  const reason = invite.invalid_reason;
  const text = (() => {
    switch (reason) {
      case "expired":
        return "这个邀请链接已过期。";
      case "used":
        return "这个邀请链接已被使用，无法再次注册。";
      case "revoked":
        return "这个邀请链接已被撤销。";
      case "not_found":
        return "邀请链接不存在。";
      default:
        if (invite.used) return "这个邀请链接已被使用，无法再次注册。";
        return reason ? `邀请不可用：${reason}` : "邀请链接不可用。";
    }
  })();
  const icon = (() => {
    switch (reason) {
      case "expired":
        return <Clock className="w-6 h-6 text-[var(--fg-2)]" />;
      case "used":
        return <Check className="w-6 h-6 text-[var(--fg-2)]" />;
      case "revoked":
        return <ShieldOff className="w-6 h-6 text-[var(--fg-2)]" />;
      case "not_found":
        return <FileX className="w-6 h-6 text-[var(--fg-2)]" />;
      default:
        return <AlertCircle className="w-6 h-6 text-[var(--fg-2)]" />;
    }
  })();

  return (
    <div className="space-y-5">
      <div className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 backdrop-blur-sm p-6 text-center space-y-3">
        <div className="mx-auto w-14 h-14 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] border border-[var(--border)] flex items-center justify-center">
          {icon}
        </div>
        <h1 className="type-section-title">邀请不可用</h1>
        <p className="text-sm text-[var(--fg-1)]">{text}</p>
        <p className="text-xs text-[var(--fg-2)]">
          如果你认为这是错误，请联系邀请你的人重新生成邀请。
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Link
          href="/login"
          className="type-control inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] transition-colors hover:bg-[var(--bg-3)]"
        >
          去登录
        </Link>
        <Link
          href="/"
          className="type-control inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] transition-colors hover:bg-[var(--bg-3)]"
        >
          返回首页
        </Link>
      </div>
    </div>
  );
}

function ErrorView({
  error,
  pending,
  onRetry,
}: {
  error: unknown;
  pending: boolean;
  onRetry: () => void;
}) {
  const isNotFound = error instanceof ApiError && error.status === 404;
  return (
    <div className="space-y-5">
      <div className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 backdrop-blur-sm p-6 text-center space-y-3">
        <div className="mx-auto w-14 h-14 rounded-[var(--radius-dialog)] bg-[var(--bg-2)] border border-[var(--border)] flex items-center justify-center">
          <FileX className="w-6 h-6 text-[var(--fg-2)]" />
        </div>
        <h1 className="type-section-title">
          {isNotFound ? "邀请不存在" : "加载邀请失败"}
        </h1>
        {!isNotFound && (
          <p className="flex items-center justify-center gap-1.5 type-caption text-danger">
            <AlertCircle className="w-3.5 h-3.5" />
            暂时无法加载邀请，请重试。
          </p>
        )}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {!isNotFound ? (
          <button
            type="button"
            onClick={onRetry}
            disabled={pending}
            className="type-control inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-strong)] bg-[var(--bg-2)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-50"
          >
            {pending && <Loader2 className="h-4 w-4 animate-spin" />}
            {pending ? "重试中" : "重试"}
          </button>
        ) : (
          <Link
            href="/login"
            className="type-control inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] transition-colors hover:bg-[var(--bg-3)]"
          >
            去登录
          </Link>
        )}
        <Link
          href="/"
          className="type-control inline-flex min-h-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] transition-colors hover:bg-[var(--bg-3)]"
        >
          返回首页
        </Link>
      </div>
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

function InfoLine({
  label,
  icon,
  children,
}: {
  label: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between text-[11px] sm:text-xs">
      <span className="inline-flex items-center gap-1.5 uppercase tracking-wider text-[var(--fg-2)]">
        {icon}
        {label}
      </span>
      <div>{children}</div>
    </div>
  );
}

function RoleBadge({ role }: { role: "admin" | "member" }) {
  if (role === "admin") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-[var(--color-lumen-amber)]/15 text-[var(--color-lumen-amber)] border border-[var(--color-lumen-amber)]/30">
        <UserCog className="w-3 h-3" />
        admin
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-xs bg-[var(--bg-2)] text-[var(--fg-1)] border border-[var(--border)]">
      <UsersIcon className="w-3 h-3" />
      member
    </span>
  );
}

// ——— 密码强度 ———

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
        {PASSWORD_STRENGTH_SEGMENTS.map((segment, i) => (
          <div
            key={segment}
            className={
              "flex-1 h-1 rounded-full transition-colors duration-200 " +
              (i < strength.score ? strength.color : "bg-[var(--bg-2)]")
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
