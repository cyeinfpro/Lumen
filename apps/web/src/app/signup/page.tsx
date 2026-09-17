"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useFormFeedback } from "@/hooks/useFormFeedback";
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
import { Select } from "@/components/ui/primitives";
import { isValidEmailInput, normalizeEmailInput } from "@/lib/email";

// review §9: 8+ BYOK 错误码 → 中文文案。signup 与绑定页共用。
const BYOK_ERROR_TEXT: Record<string, string> = {
  byok_disabled: "当前未开放 API 密钥 注册",
  invalid_api_key: "API 密钥 无效或被供应商拒绝",
  supplier_unsupported: "供应商或协议不支持",
  model_not_available: "供应商不可用此模型",
  key_rate_limited: "Key 当前被限流，稍后再试",
  supplier_transient_error: "供应商临时错误，稍后重试",
  validation_timeout: "验证超时",
  validation_wrong_answer: "供应商返回不可信，检查 Key 与供应商配置",
  invalid_supplier_response: "供应商响应格式不兼容",
  invalid_verification_token: "验证已失效，重新验证 API 密钥",
  verification_expired: "验证已过期，重新验证 API 密钥",
  verification_consumed: "验证已使用，重新验证 API 密钥",
  verification_not_found: "验证记录不存在，重新验证 API 密钥",
  email_taken: "该邮箱已注册，可直接登录",
};

// step 2 拿到 verification_* 错误码时需要清空 token 回退到 step 1。
const VERIFICATION_RESET_RE = /verification/i;

function getSignupValidationError({
  verificationToken,
  email,
  password,
  confirm,
}: {
  verificationToken: string;
  email: string;
  password: string;
  confirm: string;
}): { message: string; fieldId: string } | null {
  if (!verificationToken) return { message: "请先验证 API 密钥", fieldId: "signup-api-key" };
  if (!isValidEmailInput(email)) return { message: "邮箱格式不正确", fieldId: "signup-email" };
  if (password.length < 8) return { message: "密码至少 8 位", fieldId: "signup-password" };
  if (password !== confirm) return { message: "两次密码输入不一致", fieldId: "signup-confirm-password" };
  return null;
}

function resolveSupplierId(
  supplierId: string,
  selectedSupplierId: string | undefined,
): string {
  return supplierId || selectedSupplierId || "";
}

type PublicApiSupplier = Awaited<
  ReturnType<typeof listPublicApiSuppliers>
>["items"][number];

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
  const verificationFeedback = useFormFeedback("signup-verification-error");
  const accountFeedback = useFormFeedback("signup-account-error");
  const previousVerification = useRef("");
  useEffect(() => {
    if (previousVerification.current === verificationToken) return;
    previousVerification.current = verificationToken;
    const target = document.getElementById(verificationToken ? "signup-email" : "signup-api-key");
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "nearest", behavior: "instant" });
  }, [verificationToken]);
  const verifyGuardRef = useRef(false);
  const submitGuardRef = useRef(false);

  const selectedSupplier = useMemo(
    () => suppliers.find((supplier) => supplier.id === supplierId) ?? suppliers[0],
    [suppliers, supplierId],
  );
  const activeSupplierId = resolveSupplierId(supplierId, selectedSupplier?.id);

  const onVerify = async () => {
    verificationFeedback.clear();
    if (!activeSupplierId) {
      verificationFeedback.report("请选择供应商", "signup-supplier");
      return;
    }
    if (!apiKey.trim()) {
      verificationFeedback.report("请输入 API 密钥", "signup-api-key");
      return;
    }
    if (verifyGuardRef.current) return;
    verifyGuardRef.current = true;
    setVerifying(true);
    try {
      const result = await verifyApiKey(activeSupplierId, apiKey.trim());
      setVerificationToken(result.verification_token);
      setKeyHint(result.key_hint);
      setApiKey("");
    } catch (err) {
      setVerificationToken("");
      setKeyHint("");
      verificationFeedback.report(byokErrorText(err), "signup-api-key");
    } finally {
      verifyGuardRef.current = false;
      setVerifying(false);
    }
  };

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    accountFeedback.clear();
    const trimmedEmail = normalizeEmailInput(email);
    const validationError = getSignupValidationError({
      verificationToken,
      email: trimmedEmail,
      password,
      confirm,
    });
    if (validationError) {
      const feedback = verificationToken ? accountFeedback : verificationFeedback;
      feedback.report(validationError.message, validationError.fieldId);
      return;
    }
    if (submitGuardRef.current) return;
    submitGuardRef.current = true;
    setSubmitting(true);
    try {
      await signupByok(trimmedEmail, password, verificationToken);
      router.replace("/");
    } catch (err) {
      // step 2 token 过期 / 已用 / 不存在 → 清空 token 让用户回 step 1 重新验证
      const code = extractErrorCode(err);
      if (code && VERIFICATION_RESET_RE.test(code)) {
        setVerificationToken("");
        setKeyHint("");
        verificationFeedback.report(BYOK_ERROR_TEXT[code] ?? "验证已失效，重新验证 API 密钥", "signup-api-key");
        submitGuardRef.current = false;
        setSubmitting(false);
        return;
      }
      accountFeedback.report(byokErrorText(err));
      submitGuardRef.current = false;
      setSubmitting(false);
    }
  };

  const disabled = suppliersQ.isLoading || suppliers.length === 0;

  return (
    <div className="page-shell">
      <main className="auth-stage">
        <div className="auth-frame">
          <header className="auth-header">
            <Link
              href="/login"
              className="type-body-sm inline-flex items-center gap-1.5 hover:text-[var(--fg-0)]"
            >
              <ArrowLeft className="w-4 h-4" />
              返回登录
            </Link>
            <div className="grid gap-1.5 pt-1">
              <h1 className="type-page-title">创建 Lumen 账号</h1>
              <p className="type-body">连接你的 API 密钥 后继续注册。</p>
            </div>
          </header>

          <ApiKeyVerificationSection
            suppliers={suppliers}
            activeSupplierId={activeSupplierId}
            apiKey={apiKey}
            verificationToken={verificationToken}
            keyHint={keyHint}
            disabled={disabled}
            verifying={verifying}
            suppliersError={suppliersQ.isError}
            suppliersFetching={suppliersQ.isFetching}
            onRetry={() => void suppliersQ.refetch()}
            onSupplierChange={setSupplierId}
            onApiKeyChange={setApiKey}
            onVerify={() => void onVerify()}
            feedback={verificationFeedback}
            submitting={submitting}
            onChangeKey={() => {
              setVerificationToken("");
              setKeyHint("");
              verificationFeedback.clear();
              accountFeedback.clear();
            }}
          />

          <SignupAccountForm
            email={email}
            password={password}
            confirm={confirm}
            showPassword={showPassword}
            submitting={submitting}
            verificationToken={verificationToken}
            feedback={accountFeedback}
            onSubmit={onCreate}
            onEmailChange={setEmail}
            onPasswordChange={setPassword}
            onConfirmChange={setConfirm}
            onTogglePassword={() => setShowPassword((value) => !value)}
          />
        </div>
      </main>
    </div>
  );
}

function ApiKeyVerificationSection({
  suppliers,
  activeSupplierId,
  apiKey,
  verificationToken,
  keyHint,
  disabled,
  verifying,
  suppliersError,
  suppliersFetching,
  onRetry,
  onSupplierChange,
  onApiKeyChange,
  onVerify,
  feedback,
  submitting,
  onChangeKey,
}: {
  suppliers: PublicApiSupplier[];
  activeSupplierId: string;
  apiKey: string;
  verificationToken: string;
  keyHint: string;
  disabled: boolean;
  verifying: boolean;
  suppliersError: boolean;
  suppliersFetching: boolean;
  onRetry: () => void;
  onSupplierChange: (value: string) => void;
  onApiKeyChange: (value: string) => void;
  onVerify: () => void;
  feedback: ReturnType<typeof useFormFeedback>;
  submitting: boolean;
  onChangeKey: () => void;
}) {
  const controlsDisabled = disabled || verifying || submitting || Boolean(verificationToken);

  return (
    <form className="page-section grid gap-4 !pt-0" noValidate onInput={feedback.clear}
      aria-label="连接 API 密钥"
      onSubmit={(event) => { event.preventDefault(); if (!controlsDisabled) onVerify(); }}>
      <div className="type-label flex items-center gap-2">
        <KeyRound className="w-3.5 h-3.5" />
        连接 API 密钥
      </div>
      <SupplierLoadError
        visible={suppliersError}
        fetching={suppliersFetching}
        onRetry={onRetry}
      />
      <label className="auth-field">
        <span className="type-label">供应商</span>
        <Select
          id="signup-supplier"
          {...feedback.fieldProps("signup-supplier")}
          name="supplier"
          value={activeSupplierId}
          disabled={controlsDisabled}
          onChange={(event) => onSupplierChange(event.target.value)}
          className="auth-control"
          wrapperClassName="w-full"
        >
          {suppliers.length === 0 ? (
            <option value="">{suppliersFetching ? "正在加载供应商…" : "暂无可用供应商"}</option>
          ) : (
            suppliers.map((supplier) => (
              <option key={supplier.id} value={supplier.id}>
                {supplier.name} · {supplier.validation_model}
              </option>
            ))
          )}
        </Select>
      </label>
      <label className="auth-field">
        <span className="type-label">API 密钥</span>
        <div className="relative">
          <Server className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--fg-2)]" />
          <input
            id="signup-api-key"
            {...feedback.fieldProps("signup-api-key")}
            name="api-key"
            type="password"
            value={apiKey}
            disabled={controlsDisabled}
            onChange={(event) => onApiKeyChange(event.target.value)}
            placeholder="sk-..."
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            enterKeyHint="go"
            className="auth-control pl-10 pr-3"
          />
        </div>
      </label>
      <button
        type="submit"
        disabled={controlsDisabled}
        aria-busy={verifying}
        className="type-control inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] hover:bg-[var(--bg-3)] disabled:opacity-50"
      >
        <VerificationButtonContent
          verifying={verifying}
          verificationToken={verificationToken}
          keyHint={keyHint}
        />
      </button>
      <SignupFormError feedback={feedback} />
      {verificationToken ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p role="status" className="type-caption text-[var(--success-fg)]">密钥验证通过，继续填写账号信息。</p>
          <button type="button" onClick={onChangeKey} disabled={submitting}
            className="type-caption inline-flex min-h-11 items-center px-2 text-[var(--link-fg)] hover:underline disabled:opacity-50">
            更换 API 密钥
          </button>
        </div>
      ) : null}
    </form>
  );
}

function SupplierLoadError({
  visible,
  fetching,
  onRetry,
}: {
  visible: boolean;
  fetching: boolean;
  onRetry: () => void;
}) {
  if (!visible) return null;
  return (
    <div
      role="alert"
      aria-live="assertive"
      className="flex items-center justify-between gap-3 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger"
    >
      <span>供应商列表加载失败</span>
      <button
        type="button"
        onClick={onRetry}
        disabled={fetching}
        className="type-caption inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1 text-[var(--fg-1)] hover:bg-[var(--bg-2)] disabled:opacity-50"
      >
        {fetching ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
        ) : (
          <RefreshCw className="w-3.5 h-3.5" />
        )}
        重试
      </button>
    </div>
  );
}

function VerificationButtonContent({
  verifying,
  verificationToken,
  keyHint,
}: {
  verifying: boolean;
  verificationToken: string;
  keyHint: string;
}) {
  if (verifying) {
    return (
      <>
        <Loader2 className="w-4 h-4 animate-spin" />
        正在验证 API 密钥…
      </>
    );
  }
  if (verificationToken) {
    return (
      <>
        <Check className="w-4 h-4 text-success" />
        已验证 {keyHint}
      </>
    );
  }
  return (
    <>
      <KeyRound className="w-4 h-4" />
      验证 API 密钥
    </>
  );
}

function SignupAccountForm({
  email,
  password,
  confirm,
  showPassword,
  submitting,
  verificationToken,
  feedback,
  onSubmit,
  onEmailChange,
  onPasswordChange,
  onConfirmChange,
  onTogglePassword,
}: {
  email: string;
  password: string;
  confirm: string;
  showPassword: boolean;
  submitting: boolean;
  verificationToken: string;
  feedback: ReturnType<typeof useFormFeedback>;
  onSubmit: (event: React.FormEvent) => void;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onConfirmChange: (value: string) => void;
  onTogglePassword: () => void;
}) {
  const passwordInputType = showPassword ? "text" : "password";

  return (
    <form onSubmit={onSubmit} className="page-section auth-form" noValidate onInput={feedback.clear} aria-label="创建账号">
      <div className="type-label flex items-center gap-2">
        <Mail className="w-3.5 h-3.5" />
        创建账号
      </div>
      <label className="auth-field">
        <span className="type-label">邮箱</span>
        <input
          id="signup-email"
          {...feedback.fieldProps("signup-email")}
          name="email"
          type="email"
          disabled={submitting}
          value={email}
          onChange={(event) => onEmailChange(event.target.value)}
          placeholder="name@example.com"
          autoComplete="email"
          inputMode="email"
          autoCapitalize="none"
          autoCorrect="off"
          enterKeyHint="next"
          className="auth-control px-3"
        />
      </label>
      <div className="auth-field">
        <label htmlFor="signup-password" className="type-label">
          密码
        </label>
        <div className="relative">
          <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--fg-2)]" />
          <input
            id="signup-password"
          {...feedback.fieldProps("signup-password")}
            name="password"
            type={passwordInputType}
            disabled={submitting}
            value={password}
            onChange={(event) => onPasswordChange(event.target.value)}
            placeholder="至少 8 位密码"
            autoComplete="new-password"
            enterKeyHint="next"
            className="auth-control pl-10 pr-12"
          />
          <button
            type="button"
            onClick={onTogglePassword}
            disabled={submitting}
            className="absolute right-0 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:opacity-50"
            aria-label={showPassword ? "隐藏密码" : "显示密码"}
          >
            {showPassword ? (
              <EyeOff className="w-4 h-4" />
            ) : (
              <Eye className="w-4 h-4" />
            )}
          </button>
        </div>
      </div>
      <label className="auth-field">
        <span className="type-label">确认密码</span>
        <input
          id="signup-confirm-password"
          {...feedback.fieldProps("signup-confirm-password")}
          name="password-confirmation"
          type={passwordInputType}
          disabled={submitting}
          value={confirm}
          onChange={(event) => onConfirmChange(event.target.value)}
          placeholder="再次输入密码"
          autoComplete="new-password"
          enterKeyHint="done"
          className="auth-control px-3"
        />
      </label>

      <SignupFormError feedback={feedback} />
      {!verificationToken ? (
        <p id="signup-verification-hint" className="type-caption text-[var(--fg-1)]">
          请先在上方验证 API 密钥。已填写的账号信息会保留在本页。
        </p>
      ) : null}

      <button
        type="submit"
        disabled={submitting || !verificationToken}
        aria-describedby={!verificationToken ? "signup-verification-hint" : undefined}
        aria-busy={submitting}
        className="type-control inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[transform,background-color] hover:bg-[var(--accent-hover)] active:scale-[var(--press-scale-soft)] disabled:opacity-50"
      >
        {submitting ? (
          <>
            <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" />
            创建中…
          </>
        ) : (
          <>
            创建账号
            <ArrowRight className="w-4 h-4" />
          </>
        )}
      </button>
    </form>
  );
}

function SignupFormError({ feedback }: { feedback: ReturnType<typeof useFormFeedback> }) {
  if (!feedback.message) return null;
  return (
    <div id={feedback.errorId} tabIndex={-1} role="alert"
      className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <span>{feedback.message}</span>
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
    if (err.status === 429) return "请求过于频繁，稍后再试";
    if (err.status === 422) return "提交内容不合法";
  }
  return "请求失败，稍后重试";
}
