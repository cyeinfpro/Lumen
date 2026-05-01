"use client";

// 邀请注册页：/invite/{token}
// Next 16 dynamic params 是 Promise —— Client Component 用 React.use() 解包。
// 流程：
//  1. usePublicInviteQuery 拉 invite info（公共，不带 cookie）
//  2. valid=false 显示明确文案（按 invalid_reason）
//  3. valid=true 渲染注册表单：email（如果 invite.email 已绑定则锁定）/ password / 确认密码
//     附带一个 live 密码强度指示器
//  4. 提交 → signup(email, password, token)；成功 router.push("/")；失败 inline 显示

import { use, useMemo, useState } from "react";
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

import { usePublicInviteQuery } from "@/lib/queries";
import { ApiError, signup } from "@/lib/apiClient";
import type { InviteLinkPublicOut } from "@/lib/types";

export default function InvitePage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = use(params);
  const q = usePublicInviteQuery(token);

  return (
    <div className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200 flex flex-col">
      <main className="flex-1 flex flex-col items-center justify-center px-4 py-10 md:py-16 safe-x">
        <motion.div
          initial={false}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3, ease: "easeOut" }}
          className="w-full max-w-md"
        >
          <header className="mb-8 flex items-center gap-3">
            <span className="w-9 h-9 rounded-full bg-gradient-to-tr from-[var(--color-lumen-amber)] to-orange-200 shadow-[0_0_24px_-4px_var(--color-lumen-amber)]" />
            <div>
              <p className="text-lg font-medium tracking-tight leading-none">
                Lumen
              </p>
              <p className="text-[11px] uppercase tracking-wider text-neutral-500 mt-0.5">
                邀请注册
              </p>
            </div>
          </header>

          {q.isLoading ? (
            <SkeletonInvite />
          ) : q.isError ? (
            <ErrorView error={q.error} />
          ) : q.data ? (
            q.data.valid ? (
              <SignupForm token={token} invite={q.data} />
            ) : (
              <InvalidView invite={q.data} />
            )
          ) : null}
        </motion.div>
      </main>

      <footer className="py-4 px-4 text-center text-xs text-neutral-500 safe-bottom">
        <Link
          href="/login"
          className="hover:text-neutral-300 transition-colors"
        >
          已有账号？直接登录
        </Link>
      </footer>
    </div>
  );
}

function SkeletonInvite() {
  return (
    <div className="space-y-5">
      <div className="h-8 w-48 bg-white/5 rounded animate-pulse" />
      <div className="h-4 w-72 bg-white/5 rounded animate-pulse" />
      <div className="h-40 rounded-2xl bg-white/5 animate-pulse mt-6" />
      <div className="h-44 rounded-2xl bg-white/5 animate-pulse" />
    </div>
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

  const expiresLabel = useMemo(() => {
    if (!invite.expires_at) return "永久";
    try {
      return format(new Date(invite.expires_at), "yyyy-MM-dd HH:mm");
    } catch {
      return invite.expires_at;
    }
  }, [invite.expires_at]);

  const strength = useMemo(() => passwordStrength(password), [password]);
  const confirmMismatch = confirm.length > 0 && confirm !== password;
  const passwordTooShort = password.length < 8;
  const canSubmit =
    !submitting &&
    email.trim().length > 0 &&
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim()) &&
    !passwordTooShort &&
    confirm === password;

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
    if (password.length < 8) {
      setError("密码至少 8 位");
      return;
    }
    if (password !== confirm) {
      setError("两次密码输入不一致");
      return;
    }

    setSubmitting(true);
    try {
      await signup(trimmedEmail, password, token);
      router.push("/");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          setError("该邮箱已注册，请直接登录");
        } else if (err.status === 403) {
          setError(err.message || "邀请被拒绝（可能与登录邮箱不一致）");
        } else if (err.status === 410 || err.status === 404) {
          setError("邀请已失效或不存在");
        } else if (err.status === 422) {
          setError(err.message || "提交内容不合法");
        } else {
          setError(err.message || `注册失败 (HTTP ${err.status})`);
        }
      } else if (err instanceof Error) {
        setError(err.message || "注册失败");
      } else {
        setError("注册失败");
      }
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">
          加入 Lumen
        </h1>
        <p className="text-sm text-[var(--fg-1)] mt-1.5">
          使用邀请链接创建你的账号。
        </p>
      </div>

      {/* 邀请信息 */}
      <div className="rounded-2xl border border-[var(--color-lumen-amber)]/30 bg-[var(--color-lumen-amber)]/[0.05] backdrop-blur-sm p-4 space-y-2">
        <div className="flex items-center gap-1.5 text-xs uppercase tracking-wider text-[var(--color-lumen-amber)]">
          <Sparkles className="w-3.5 h-3.5" /> 邀请详情
        </div>
        <InfoLine label="角色" icon={<UserCog className="w-3 h-3" />}>
          <RoleBadge role={invite.role} />
        </InfoLine>
        {invite.email && (
          <InfoLine label="绑定邮箱" icon={<Mail className="w-3 h-3" />}>
            <span className="text-neutral-200">{invite.email}</span>
          </InfoLine>
        )}
        <InfoLine label="过期" icon={<Clock className="w-3 h-3" />}>
          <span className="text-neutral-200 font-mono tabular-nums text-xs">
            {expiresLabel}
          </span>
        </InfoLine>
      </div>

      <form onSubmit={onSubmit} className="space-y-4">
        <Field id="invite-email" label="邮箱" icon={<Mail className="w-3.5 h-3.5" />}>
          <input
            id="invite-email"
            type="email"
            required
            readOnly={lockedEmail}
            value={email}
            onChange={(e) => {
              if (lockedEmail) return;
              setEmailInput(e.target.value);
            }}
            placeholder="you@example.com"
            autoComplete="email"
            className={
              "w-full h-10 px-3 rounded-xl bg-[var(--bg-1)]/60 border border-white/10 text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 placeholder:text-neutral-600 transition-colors " +
              (lockedEmail ? "opacity-70 cursor-not-allowed" : "")
            }
          />
          {lockedEmail && (
            <p className="text-[11px] text-neutral-500 mt-1">
              该邀请已绑定此邮箱，不能修改。
            </p>
          )}
        </Field>

        <Field
          id="invite-password"
          label="密码"
          icon={<Lock className="w-3.5 h-3.5" />}
        >
          <div className="relative">
            <input
              id="invite-password"
              type={showPwd ? "text" : "password"}
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 8 位"
              autoComplete="new-password"
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
          id="invite-confirm"
          label="确认密码"
          icon={<KeyRound className="w-3.5 h-3.5" />}
        >
          <input
            id="invite-confirm"
            type={showPwd ? "text" : "password"}
            required
            minLength={8}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="再输入一次"
            autoComplete="new-password"
            className={
              "w-full h-10 px-3 rounded-xl bg-[var(--bg-1)]/60 border text-base md:text-sm focus:outline-none focus:ring-2 placeholder:text-neutral-600 transition-colors " +
              (confirmMismatch
                ? "border-red-500/40 focus:border-red-500/60 focus:ring-red-500/20"
                : "border-white/10 focus:border-[var(--color-lumen-amber)]/50 focus:ring-[var(--color-lumen-amber)]/25")
            }
          />
          {confirmMismatch && (
            <p className="flex items-center gap-1 text-xs text-red-300 mt-1.5">
              <AlertCircle className="w-3 h-3" /> 两次输入不一致
            </p>
          )}
        </Field>

        <AnimatePresence>
          {error && (
            <motion.div
              initial={{ opacity: 0, y: -2 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/5 px-3 py-2 text-sm text-red-300"
            >
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              {error}
            </motion.div>
          )}
        </AnimatePresence>

        <button
          type="submit"
          disabled={!canSubmit}
          className="w-full inline-flex items-center justify-center gap-1.5 h-11 sm:h-10 px-5 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.98] text-black text-sm font-medium disabled:opacity-50 transition-all shadow-[0_8px_24px_-12px_var(--color-lumen-amber)]"
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

      <p className="text-xs text-neutral-500 text-center">
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
        return <Clock className="w-6 h-6 text-neutral-400" />;
      case "used":
        return <Check className="w-6 h-6 text-neutral-400" />;
      case "revoked":
        return <ShieldOff className="w-6 h-6 text-neutral-400" />;
      case "not_found":
        return <FileX className="w-6 h-6 text-neutral-400" />;
      default:
        return <AlertCircle className="w-6 h-6 text-neutral-400" />;
    }
  })();

  return (
    <div className="space-y-5">
      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 backdrop-blur-sm p-6 text-center space-y-3">
        <div className="mx-auto w-14 h-14 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center">
          {icon}
        </div>
        <h1 className="text-xl font-semibold tracking-tight">邀请不可用</h1>
        <p className="text-sm text-[var(--fg-1)]">{text}</p>
        <p className="text-xs text-neutral-500">
          如果你认为这是错误，请联系邀请你的人重新生成邀请。
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Link
          href="/login"
          className="h-10 inline-flex items-center justify-center rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-sm transition-colors"
        >
          去登录
        </Link>
        <Link
          href="/"
          className="h-10 inline-flex items-center justify-center rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-sm transition-colors"
        >
          返回首页
        </Link>
      </div>
    </div>
  );
}

function ErrorView({ error }: { error: unknown }) {
  const isNotFound = error instanceof ApiError && error.status === 404;
  const message = error instanceof Error ? error.message : "未知错误";
  return (
    <div className="space-y-5">
      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 backdrop-blur-sm p-6 text-center space-y-3">
        <div className="mx-auto w-14 h-14 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center">
          <FileX className="w-6 h-6 text-neutral-400" />
        </div>
        <h1 className="text-xl font-semibold tracking-tight">
          {isNotFound ? "邀请不存在" : "加载邀请失败"}
        </h1>
        {!isNotFound && (
          <p className="flex items-center justify-center gap-1.5 text-xs text-red-300">
            <AlertCircle className="w-3.5 h-3.5" /> {message}
          </p>
        )}
      </div>
      <Link
        href="/"
        className="h-10 w-full inline-flex items-center justify-center rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-sm transition-colors"
      >
        返回首页
      </Link>
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
      <span className="inline-flex items-center gap-1.5 uppercase tracking-wider text-neutral-500">
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
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-[var(--color-lumen-amber)]/15 text-[var(--color-lumen-amber)] border border-[var(--color-lumen-amber)]/30">
        <UserCog className="w-3 h-3" />
        admin
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-white/5 text-neutral-400 border border-white/10">
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
        {PASSWORD_STRENGTH_SEGMENTS.map((segment, i) => (
          <div
            key={segment}
            className={
              "flex-1 h-1 rounded-full transition-colors duration-200 " +
              (i < strength.score ? strength.color : "bg-white/8")
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
