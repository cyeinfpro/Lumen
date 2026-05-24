"use client";

// Telegram 机器人设置：复用 system_settings 接口（telegram.* 系列 key）。
// 表单分两块：
//   - 基本：开关 / token / username / 白名单
//   - 代理：proxy_names（多选）+ strategy（4 选 1）
// 保存后提示需要重启 lumen-tgbot 才生效；重启按钮暂不实现，显示命令提示给运维。

import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertCircle,
  AlertTriangle,
  Bot,
  Check,
  Copy,
  Eye,
  EyeOff,
  Network,
  RotateCw,
  Save,
  Shuffle,
  X,
  Zap,
} from "lucide-react";

import {
  useAdminProxiesQuery,
  useRestartTelegramBotMutation,
  useSystemSettingsQuery,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";

const SETTINGS_KEYS = [
  "telegram.bot_enabled",
  "telegram.bot_token",
  "telegram.bot_username",
  "telegram.allowed_user_ids",
  "telegram.proxy_names",
  "telegram.proxy_strategy",
] as const;

const STRATEGIES: {
  value: string;
  label: string;
  hint: string;
  icon: React.ComponentType<{ className?: string }>;
}[] = [
  {
    value: "random",
    label: "随机",
    hint: "每次请求随机选一个，分摊压力。",
    icon: Shuffle,
  },
  {
    value: "latency",
    label: "选最快",
    hint: "从延迟最低的一组里随机选。",
    icon: Zap,
  },
  {
    value: "failover",
    label: "主备",
    hint: "永远用列表第一个，挂了才切到下一个。",
    icon: Network,
  },
  {
    value: "round_robin",
    label: "轮流",
    hint: "按顺序轮换使用。",
    icon: Shuffle,
  },
];

export function TelegramPanel() {
  const settingsQuery = useSystemSettingsQuery();
  const proxiesQuery = useAdminProxiesQuery();
  const updateMut = useUpdateSystemSettingsMutation();
  const restartMut = useRestartTelegramBotMutation();

  const initial = useMemo(() => {
    const items = settingsQuery.data?.items ?? [];
    const get = (key: string) => items.find((it) => it.key === key)?.value ?? "";
    return {
      bot_enabled: get("telegram.bot_enabled") || "1",
      bot_token: get("telegram.bot_token"),
      bot_username: get("telegram.bot_username"),
      allowed_user_ids: get("telegram.allowed_user_ids"),
      proxy_names: get("telegram.proxy_names"),
      proxy_strategy: get("telegram.proxy_strategy") || "random",
    };
  }, [settingsQuery.data]);

  const [form, setForm] = useState(initial);
  // initial 由 useMemo 派生，server settings 变化时重置 form（React 19 推荐：render 期间检测）
  const [prevInitial, setPrevInitial] = useState(initial);
  if (prevInitial !== initial) {
    setPrevInitial(initial);
    setForm(initial);
  }

  const [showToken, setShowToken] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restartPrompt, setRestartPrompt] = useState(false);
  const [restartHint, setRestartHint] = useState<string | null>(null);

  const dirty = useMemo(() => {
    return SETTINGS_KEYS.some((k) => {
      const key = k.replace("telegram.", "") as keyof typeof form;
      return form[key] !== initial[key];
    });
  }, [form, initial]);

  const allProxies = proxiesQuery.data?.items ?? [];
  const selectedProxyNames = useMemo(() => {
    const set = new Set(
      form.proxy_names.split(",").map((s) => s.trim()).filter(Boolean),
    );
    return set;
  }, [form.proxy_names]);

  const toggleProxy = (name: string) => {
    const next = new Set(selectedProxyNames);
    if (next.has(name)) {
      next.delete(name);
    } else {
      next.add(name);
    }
    setForm((f) => ({
      ...f,
      proxy_names: Array.from(next).join(","),
    }));
  };

  const onSave = async () => {
    setError(null);
    setRestartHint(null);
    try {
      await updateMut.mutateAsync(
        SETTINGS_KEYS.map((k) => {
          const fieldKey = k.replace("telegram.", "") as keyof typeof form;
          return { key: k, value: form[fieldKey] };
        }),
      );
      setSavedAt(Date.now());
      // 保存成功 → 弹窗问是否立即重启 bot
      setRestartPrompt(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : copy.error.unknown);
    }
  };

  const onConfirmRestart = async () => {
    setRestartHint(null);
    try {
      const res = await restartMut.mutateAsync();
      if (res.receivers === 0) {
        setRestartHint(
          "已发送重启指令，但当前没有 bot 进程在监听控制通道（可能未启动）。请手动 systemctl start lumen-tgbot。",
        );
      } else {
        setRestartHint(`已发送重启指令，机器人会在数秒内自动重新启动。`);
      }
    } catch (err) {
      setRestartHint(err instanceof Error ? err.message : copy.error.unknown);
    } finally {
      setRestartPrompt(false);
    }
  };

  const deepLink = form.bot_username
    ? `https://t.me/${form.bot_username}?start=`
    : "";

  return (
    <section className="space-y-5">
      {/* 提示条 */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-dialog)] p-4 md:p-5">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-[var(--radius-card)] bg-white/5 border border-[var(--border)] flex items-center justify-center shrink-0">
            <Bot className="w-4 h-4 text-[var(--fg-2)]" />
          </div>
          <div className="min-w-0 type-caption text-[var(--fg-2)] leading-relaxed">
            <p className="type-card-title mb-1">Telegram 机器人</p>
            保存后可直接重启机器人。代理切换不需要重启；发送失败时会自动换下一个。
          </div>
        </div>
      </div>

      {/* 基本 */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-dialog)] p-4 md:p-5 space-y-4">
        <h3 className="type-card-title">基本设置</h3>

        <ToggleField
          label="启用机器人"
          hint="关闭后机器人会在启动时退出。临时停服可直接关掉这里。"
          on={form.bot_enabled === "1"}
          onChange={(v) =>
            setForm((f) => ({ ...f, bot_enabled: v ? "1" : "0" }))
          }
        />

        <Field
          label="机器人令牌"
          hint="@BotFather 申请机器人时给的密钥，形如 123456789:REPLACE_WITH_BOT_TOKEN。留空时回退到部署 .env 里的旧值。"
          value={form.bot_token}
          onChange={(v) => setForm((f) => ({ ...f, bot_token: v }))}
          masked={!showToken}
          onToggleMask={() => setShowToken((s) => !s)}
          mono
        />

        <Field
          label="机器人用户名"
          hint="不带 @，比如 lumenimagebot。生成绑定码时用它拼跳转链接。"
          value={form.bot_username}
          onChange={(v) => setForm((f) => ({ ...f, bot_username: v }))}
          mono
          rightSlot={
            deepLink ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  void navigator.clipboard.writeText(deepLink + "<code>");
                }}
                title={deepLink + "<code>"}
                leftIcon={<Copy className="w-3 h-3" />}
                className="h-7 text-[11px]"
              >
                复制绑定链接
              </Button>
            ) : null
          }
        />

        <Field
          label="允许使用的 Telegram 账号编号"
          hint="只允许这些 Telegram 账号编号使用，多个用半角逗号分开。留空表示不限制。"
          value={form.allowed_user_ids}
          onChange={(v) => setForm((f) => ({ ...f, allowed_user_ids: v }))}
          mono
        />
      </div>

      {/* 代理 */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-dialog)] p-4 md:p-5 space-y-4">
        <div>
          <h3 className="type-card-title">代理设置</h3>
          <p className="type-caption text-[var(--fg-2)] mt-0.5">
            设置机器人发消息时使用哪些代理，以及选择顺序。代理本身在
            「代理池」标签页查看。
          </p>
        </div>

        <div>
          <p className="type-caption text-[var(--fg-1)] mb-2">使用哪些代理</p>
          <p className="text-[11px] text-[var(--fg-2)] mb-3">
            勾选要用的代理；一个都不勾表示用所有「启用」的代理。
          </p>
          {allProxies.length === 0 ? (
            <p className="type-caption text-[var(--fg-2)]">代理池为空，请先到供应商标签页添加代理。</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {allProxies.map((p) => {
                const sel = selectedProxyNames.has(p.name);
                return (
                  // pill 形态多选 chip（rounded-full + 多语义），不适合 Button primitive
                  <button
                    key={p.name}
                    type="button"
                    onClick={() => toggleProxy(p.name)}
                    className={
                      "inline-flex items-center gap-1.5 h-8 px-3 rounded-full border text-xs transition-colors " +
                      (sel
                        ? "bg-[var(--color-lumen-amber)]/15 border-[var(--color-lumen-amber)]/40 text-[var(--color-lumen-amber)]"
                        : "bg-white/5 border-[var(--border)] text-[var(--fg-1)] hover:bg-white/10")
                    }
                    disabled={!p.enabled}
                    title={p.enabled ? p.host + ":" + p.port : "（已禁用，不能选）"}
                  >
                    {sel && <Check className="w-3 h-3" />}
                    <span className="font-mono">{p.name}</span>
                    <span className="text-[10px] uppercase opacity-70">{p.type}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div>
          <p className="type-caption text-[var(--fg-1)] mb-2">挑选策略</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {STRATEGIES.map((s) => {
              const Icon = s.icon;
              const active = form.proxy_strategy === s.value;
              return (
                // 4 选 1 卡片选项，多行内容不适合 Button primitive
                <button
                  key={s.value}
                  type="button"
                  onClick={() => setForm((f) => ({ ...f, proxy_strategy: s.value }))}
                  className={
                    "text-left p-3 rounded-[var(--radius-card)] border text-xs transition-colors " +
                    (active
                      ? "bg-[var(--color-lumen-amber)]/10 border-[var(--color-lumen-amber)]/40"
                      : "bg-white/[0.02] border-[var(--border)] hover:bg-white/[0.05]")
                  }
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Icon className="w-3.5 h-3.5 text-[var(--fg-1)]" />
                    <span
                      className={
                        "text-sm " +
                        (active ? "text-[var(--color-lumen-amber)] font-medium" : "text-[var(--fg-0)]")
                      }
                    >
                      {s.label}
                    </span>
                  </div>
                  <p className="text-[11px] text-[var(--fg-2)] leading-relaxed">{s.hint}</p>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* 保存 */}
      <div className="flex items-center gap-3 flex-wrap">
        <Button
          variant="primary"
          size="md"
          onClick={onSave}
          disabled={!dirty || updateMut.isPending}
          loading={updateMut.isPending}
          leftIcon={!updateMut.isPending ? <Save className="w-3.5 h-3.5" /> : undefined}
        >
          {updateMut.isPending ? copy.state.saving : dirty ? "保存修改" : "无修改"}
        </Button>
        {savedAt && !restartPrompt && (
          <motion.span
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="inline-flex items-center gap-1 type-caption text-success"
          >
            <Check className="w-3 h-3" /> {copy.state.saved}
          </motion.span>
        )}
        {restartHint && (
          <span className="inline-flex items-center gap-1 type-caption text-info">
            <RotateCw className="w-3 h-3" /> {restartHint}
          </span>
        )}
        {error && (
          <span className="inline-flex items-center gap-1 type-caption text-danger">
            <AlertCircle className="w-3 h-3" /> {error}
          </span>
        )}
      </div>

      {/* 保存后弹窗：是否立即重启 bot */}
      <AnimatePresence>
        {restartPrompt && (
          <RestartConfirmModal
            pending={restartMut.isPending}
            onConfirm={() => void onConfirmRestart()}
            onCancel={() => setRestartPrompt(false)}
          />
        )}
      </AnimatePresence>
    </section>
  );
}

// ———————————————— 重启确认弹窗 ————————————————

function RestartConfirmModal({
  pending,
  onConfirm,
  onCancel,
}: {
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  // 点遮罩取消、ESC 取消。移动端贴底，避免系统弹窗式居中卡片压缩可触控区域。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !pending) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, onCancel]);

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/60 backdrop-blur-sm mobile-dialog-shell sm:items-center"
      onClick={pending ? undefined : onCancel}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.18 }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="restart-telegram-title"
        className="mobile-dialog-panel mobile-dialog-scroll w-full max-w-md overflow-y-auto rounded-t-[var(--radius-dialog)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] p-5 shadow-[var(--shadow-3)] sm:rounded-[var(--radius-dialog)] sm:border-b sm:pb-5"
      >
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-[var(--radius-card)] bg-warning-soft border border-warning-border flex items-center justify-center shrink-0">
            <AlertTriangle className="w-5 h-5 text-warning" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 id="restart-telegram-title" className="type-card-title">设置已保存，是否立即重启机器人？</h3>
            <p className="type-body-sm text-[var(--fg-2)] mt-1.5 leading-relaxed">
              重启大约需要 3 秒。期间机器人会暂时无响应；进行中的任务可在重启后通过任务列表查看。
            </p>
          </div>
        </div>
        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button
            variant="secondary"
            size="md"
            onClick={onCancel}
            disabled={pending}
            leftIcon={<X className="w-3.5 h-3.5" />}
          >
            暂不重启
          </Button>
          <Button
            variant="primary"
            size="md"
            onClick={onConfirm}
            disabled={pending}
            loading={pending}
            leftIcon={!pending ? <RotateCw className="w-3.5 h-3.5" /> : undefined}
          >
            {pending ? "正在重启" : "立即重启"}
          </Button>
        </div>
      </motion.div>
    </motion.div>
  );
}

function Field({
  label,
  hint,
  value,
  onChange,
  masked,
  onToggleMask,
  mono,
  rightSlot,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  masked?: boolean;
  onToggleMask?: () => void;
  mono?: boolean;
  rightSlot?: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="type-caption text-[var(--fg-1)]">{label}</span>
      <div className="relative">
        <input
          type={masked ? "password" : "text"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete="off"
          className={
            "w-full h-9 pr-20 pl-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/60 border border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 outline-none text-sm transition-colors " +
            (mono ? "font-mono" : "")
          }
        />
        {onToggleMask && (
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onToggleMask}
            aria-label={masked ? "显示" : "隐藏"}
            className="absolute right-2 top-1/2 -translate-y-1/2 w-7 h-7 bg-white/5 hover:bg-white/10"
          >
            {masked ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
          </IconButton>
        )}
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[11px] text-[var(--fg-2)] leading-relaxed flex-1">{hint}</span>
        {rightSlot}
      </div>
    </label>
  );
}

function ToggleField({
  label,
  hint,
  on,
  onChange,
}: {
  label: string;
  hint: string;
  on: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-start gap-3">
      {/* 自定义 switch primitive，不在 IconButton/Button 范围内 */}
      <button
        type="button"
        role="switch"
        aria-checked={on}
        onClick={() => onChange(!on)}
        className={
          "shrink-0 mt-0.5 w-11 h-6 rounded-full transition-colors relative " +
          (on
            ? "bg-[var(--color-lumen-amber)]"
            : "bg-white/10 border border-[var(--border)]")
        }
      >
        <span
          className={
            "absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform " +
            (on ? "translate-x-5" : "translate-x-0.5")
          }
        />
      </button>
      <div className="min-w-0">
        <p className="type-body-sm text-[var(--fg-0)]">{label}</p>
        <p className="text-[11px] text-[var(--fg-2)] leading-relaxed">{hint}</p>
      </div>
    </div>
  );
}
