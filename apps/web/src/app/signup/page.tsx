"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Lock,
  Mail,
  RefreshCw,
  Server,
} from "lucide-react";

import {
  ApiError,
  listPublicApiSuppliers,
  signupByok,
  verifyApiKey,
} from "@/lib/apiClient";
import { isValidEmailInput, normalizeEmailInput } from "@/lib/email";

// review §9: 8+ BYOK 错误码 → 中文文案。signup 与绑定页共用。
const BYOK_ERROR_TEXT: Record<string, string> = {
  byok_disabled: "当前未开放 API Key 注册",
  invalid_api_key: "API Key 无效或被供应商拒绝",
  supplier_unsupported: "供应商或协议不支持",
  model_not_available: "供应商不可用此模型",
  key_rate_limited: "Key 当前被限流，稍后再试",
  supplier_transient_error: "供应商临时错误，请稍后重试",
  validation_timeout: "验证超时",
  validation_wrong_answer: "供应商返回不可信，请检查 Key 与供应商配置",
  invalid_supplier_response: "供应商响应格式不兼容",
  invalid_verification_token: "验证已失效，请重新验证 API Key",
  verification_expired: "验证已过期，请重新验证 API Key",
  verification_consumed: "验证已使用，请重新验证 API Key",
  verification_not_found: "验证记录不存在，请重新验证 API Key",
  email_taken: "该邮箱已注册，请直接登录",
};

// step 2 拿到 verification_* 错误码时需要清空 token 回退到 step 1。
const VERIFICATION_RESET_RE = /verification/i;

export default function SignupPage() {
  const router = useRouter();
  const suppliersQ = useQuery({
    queryKey: ["auth", "api-suppliers"],
    queryFn: listPublicApiSuppliers,
    retry: false,
  });
  const suppliers = useMemo(
    () => suppliersQ.data?.items ?? [],
    [suppliersQ.data?.items],
  );
  const [supplierId, setSupplierId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [verificationToken, setVerificationToken] = useState("");
  const [keyHint, setKeyHint] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedSupplier = useMemo(
    () => suppliers.find((supplier) => supplier.id === supplierId) ?? suppliers[0],
    [suppliers, supplierId],
  );
  const activeSupplierId = supplierId || selectedSupplier?.id || "";

  const onVerify = async () => {
    setError(null);
    if (!activeSupplierId) {
      setError("供应商未选");
      return;
    }
    if (!apiKey.trim()) {
      setError("API Key 未填");
      return;
    }
    setVerifying(true);
    try {
      const result = await verifyApiKey(activeSupplierId, apiKey.trim());
      setVerificationToken(result.verification_token);
      setKeyHint(result.key_hint);
    } catch (err) {
      setVerificationToken("");
      setKeyHint("");
      setError(byokErrorText(err));
    } finally {
      setVerifying(false);
    }
  };

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!verificationToken) {
      setError("API Key 未验证");
      return;
    }
    const trimmedEmail = normalizeEmailInput(email);
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
    setSubmitting(true);
    try {
      await signupByok(trimmedEmail, password, verificationToken);
      router.push("/");
    } catch (err) {
      // step 2 token 过期 / 已用 / 不存在 → 清空 token 让用户回 step 1 重新验证
      const code = extractErrorCode(err);
      if (code && VERIFICATION_RESET_RE.test(code)) {
        setVerificationToken("");
        setKeyHint("");
        setError(BYOK_ERROR_TEXT[code] ?? "验证已失效，请重新验证 API Key");
        setSubmitting(false);
        return;
      }
      setError(byokErrorText(err));
      setSubmitting(false);
    }
  };

  const disabled = suppliersQ.isLoading || suppliers.length === 0;

  return (
    <div className="min-h-[100dvh] bg-[var(--bg-0)] text-[var(--fg-0)] flex flex-col">
      <main className="flex-1 flex items-center justify-center px-4 py-10 safe-x">
        <div className="w-full max-w-md space-y-7">
          <header className="space-y-2">
            <Link
              href="/login"
              className="inline-flex items-center gap-1.5 text-sm text-[var(--fg-2)] hover:text-[var(--fg-0)]"
            >
              <ArrowLeft className="w-4 h-4" />
              返回登录
            </Link>
            <h1 className="type-page-title">创建 Lumen 账号</h1>
            <p className="type-body">连接你的 API Key 后继续注册。</p>
          </header>

          <section className="space-y-4">
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
              <KeyRound className="w-3.5 h-3.5" />
              连接 API Key
            </div>
            {suppliersQ.isError && (
              <div
                role="alert"
                aria-live="assertive"
                className="flex items-center justify-between gap-3 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger"
              >
                <span>供应商列表加载失败</span>
                <button
                  type="button"
                  onClick={() => void suppliersQ.refetch()}
                  disabled={suppliersQ.isFetching}
                  className="inline-flex items-center gap-1 rounded-lg border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1 text-xs text-[var(--fg-1)] hover:bg-[var(--bg-2)] disabled:opacity-50"
                >
                  {suppliersQ.isFetching ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="w-3.5 h-3.5" />
                  )}
                  重试
                </button>
              </div>
            )}
            <label className="block space-y-1.5">
              <span className="text-xs text-[var(--fg-1)]">供应商</span>
              <select
                value={activeSupplierId}
                disabled={disabled || verifying || Boolean(verificationToken)}
                onChange={(e) => setSupplierId(e.target.value)}
                className="w-full h-10 px-3 rounded-xl bg-[var(--bg-1)] border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
              >
                {suppliers.length === 0 ? (
                  <option value="">暂无可用供应商</option>
                ) : (
                  suppliers.map((supplier) => (
                    <option key={supplier.id} value={supplier.id}>
                      {supplier.name} · {supplier.validation_model}
                    </option>
                  ))
                )}
              </select>
            </label>
            <label className="block space-y-1.5">
              <span className="text-xs text-[var(--fg-1)]">API Key</span>
              <div className="relative">
                <Server className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--fg-2)]" />
                <input
                  type="password"
                  value={apiKey}
                  disabled={verifying || Boolean(verificationToken)}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-..."
                  autoComplete="off"
                  className="w-full h-10 pl-10 pr-3 rounded-xl bg-[var(--bg-1)] border border-[var(--border)] text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
                />
              </div>
            </label>
            <button
              type="button"
              onClick={onVerify}
              disabled={disabled || verifying || Boolean(verificationToken)}
              className="w-full h-10 inline-flex items-center justify-center gap-2 rounded-xl bg-[var(--bg-1)] hover:bg-[var(--bg-2)] border border-[var(--border)] text-sm disabled:opacity-50"
            >
              {verifying ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : verificationToken ? (
                <Check className="w-4 h-4 text-success" />
              ) : (
                <KeyRound className="w-4 h-4" />
              )}
              {verificationToken ? `已验证 ${keyHint}` : "验证 Key"}
            </button>
          </section>

          <form onSubmit={onCreate} className="space-y-4">
            <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
              <Mail className="w-3.5 h-3.5" />
              创建账号
            </div>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@example.com"
              autoComplete="email"
              className="w-full h-10 px-3 rounded-xl bg-[var(--bg-1)] border border-[var(--border)] text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
            />
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--fg-2)]" />
              <input
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="至少 8 位密码"
                autoComplete="new-password"
                className="w-full h-10 pl-10 pr-11 rounded-xl bg-[var(--bg-1)] border border-[var(--border)] text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
              />
              <button
                type="button"
                onClick={() => setShowPassword((value) => !value)}
                className="absolute right-1 top-1/2 -translate-y-1/2 w-9 h-9 rounded-lg flex items-center justify-center text-[var(--fg-2)] hover:text-[var(--fg-0)]"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            <input
              type={showPassword ? "text" : "password"}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="确认密码"
              autoComplete="new-password"
              className="w-full h-10 px-3 rounded-xl bg-[var(--bg-1)] border border-[var(--border)] text-base md:text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50"
            />

            {error && (
              <div
                role="alert"
                aria-live="assertive"
                className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger"
              >
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={submitting || !verificationToken}
              className="w-full h-11 inline-flex items-center justify-center gap-2 rounded-xl bg-[var(--color-lumen-amber)] text-[var(--accent-on)] text-sm font-medium disabled:opacity-50"
            >
              {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : "创建账号"}
              {!submitting && <ArrowRight className="w-4 h-4" />}
            </button>
          </form>
        </div>
      </main>
    </div>
  );
}

// FastAPI HTTPException 在 http.ts 中已被解析为 ApiError(code, message, status)。
// 但极端情况下（响应非 JSON / 直传 detail 对象），保留兜底解析。
function extractErrorCode(err: unknown): string | null {
  if (err instanceof ApiError) return err.code || null;
  if (err && typeof err === "object" && "detail" in err) {
    const d = (err as { detail?: { error?: { code?: string } } }).detail;
    return d?.error?.code ?? null;
  }
  return null;
}

function byokErrorText(err: unknown): string {
  const code = extractErrorCode(err);
  if (code && BYOK_ERROR_TEXT[code]) return BYOK_ERROR_TEXT[code];
  if (err instanceof ApiError) {
    return err.message || `请求失败 (HTTP ${err.status})`;
  }
  return err instanceof Error ? err.message : "请求失败";
}
