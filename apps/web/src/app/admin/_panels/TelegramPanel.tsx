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
  Loader2,
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
      setError(err instanceof Error ? err.message : "保存失败");
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
      setRestartHint(err instanceof Error ? err.message : "重启失败");
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
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl p-4 md:p-5">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center shrink-0">
            <Bot className="w-4 h-4 text-neutral-400" />
          </div>
          <div className="min-w-0 text-xs text-neutral-400 leading-relaxed">
            <p className="text-sm font-medium text-neutral-100 mb-1">Telegram 机器人</p>
            保存后可直接重启机器人。代理切换不需要重启；发送失败时会自动换下一个。
          </div>
        </div>
      </div>

      {/* 基本 */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl p-4 md:p-5 space-y-4">
        <h3 className="text-sm font-medium text-neutral-100">基本设置</h3>

        <ToggleField
          label="启用机器人"
          hint="关闭后机器人会在启动时退出。临时停服可直接关掉这里。"
          on={form.bot_enabled === "1"}
          onChange={(v) =>
            setForm((f) => ({ ...f, bot_enabled: v ? "1" : "0" }))
          }
        />

        <Field
          label="Bot Token"
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
              <button
                type="button"
                onClick={() => {
                  void navigator.clipboard.writeText(deepLink + "<code>");
                }}
                className="inline-flex items-center gap-1 text-[11px] px-2 h-7 rounded-md bg-white/5 hover:bg-white/10 text-neutral-300 border border-white/10 transition-colors"
                title={deepLink + "<code>"}
              >
                <Copy className="w-3 h-3" /> 复制 deep link
              </button>
            ) : null
          }
        />

        <Field
          label="允许使用的 TG 账号 ID"
          hint="只允许这些 Telegram 账号 ID 使用，多个用英文逗号分开。留空表示不限制。"
          value={form.allowed_user_ids}
          onChange={(v) => setForm((f) => ({ ...f, allowed_user_ids: v }))}
          mono
        />
      </div>

      {/* 代理 */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl p-4 md:p-5 space-y-4">
        <div>
          <h3 className="text-sm font-medium text-neutral-100">代理设置</h3>
          <p className="text-xs text-neutral-500 mt-0.5">
            设置机器人发消息时使用哪些代理，以及选择顺序。代理本身在
            「代理池」标签页查看。
          </p>
        </div>

        <div>
          <p className="text-xs text-neutral-300 mb-2">使用哪些代理</p>
          <p className="text-[11px] text-neutral-500 mb-3">
            勾选要用的代理；一个都不勾表示用所有「启用」的代理。
          </p>
          {allProxies.length === 0 ? (
            <p className="text-xs text-neutral-500">代理池为空，请先到 Provider 标签页添加代理。</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {allProxies.map((p) => {
                const sel = selectedProxyNames.has(p.name);
                return (
                  <button
                    key={p.name}
                    type="button"
                    onClick={() => toggleProxy(p.name)}
                    className={
                      "inline-flex items-center gap-1.5 h-8 px-3 rounded-full border text-xs transition-colors " +
                      (sel
                        ? "bg-[var(--color-lumen-amber)]/15 border-[var(--color-lumen-amber)]/40 text-[var(--color-lumen-amber)]"
                        : "bg-white/5 border-white/10 text-neutral-300 hover:bg-white/10")
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
          <p className="text-xs text-neutral-300 mb-2">挑选策略</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {STRATEGIES.map((s) => {
              const Icon = s.icon;
              const active = form.proxy_strategy === s.value;
              return (
                <button
                  key={s.value}
                  type="button"
                  onClick={() => setForm((f) => ({ ...f, proxy_strategy: s.value }))}
                  className={
                    "text-left p-3 rounded-xl border text-xs transition-colors " +
                    (active
                      ? "bg-[var(--color-lumen-amber)]/10 border-[var(--color-lumen-amber)]/40"
                      : "bg-white/[0.02] border-white/10 hover:bg-white/[0.05]")
                  }
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Icon className="w-3.5 h-3.5 text-neutral-300" />
                    <span
                      className={
                        "text-sm " +
                        (active ? "text-[var(--color-lumen-amber)] font-medium" : "text-neutral-100")
                      }
                    >
                      {s.label}
                    </span>
                  </div>
                  <p className="text-[11px] text-neutral-500 leading-relaxed">{s.hint}</p>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* 保存 */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          type="button"
          onClick={onSave}
          disabled={!dirty || updateMut.isPending}
          className="inline-flex min-h-11 items-center justify-center gap-1.5 px-4 sm:h-9 sm:min-h-0 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.97] text-black text-sm font-medium disabled:opacity-50 transition-all"
        >
          <Save className="w-3.5 h-3.5" />
          {updateMut.isPending ? "保存中…" : dirty ? "保存修改" : "无修改"}
        </button>
        {savedAt && !restartPrompt && (
          <motion.span
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="inline-flex items-center gap-1 text-xs text-emerald-300"
          >
            <Check className="w-3 h-3" /> 已保存
          </motion.span>
        )}
        {restartHint && (
          <span className="inline-flex items-center gap-1 text-xs text-sky-300">
            <RotateCw className="w-3 h-3" /> {restartHint}
          </span>
        )}
        {error && (
          <span className="inline-flex items-center gap-1 text-xs text-red-300">
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
  // 简单 modal：fixed 全屏遮罩 + 中央卡片。点遮罩取消、ESC 取消。
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
      className="fixed inset-0 z-50 flex items-center justify-center px-4 bg-black/60 backdrop-blur-sm"
      onClick={pending ? undefined : onCancel}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.18 }}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-2xl bg-[var(--bg-1)] border border-white/10 p-5 shadow-[0_30px_60px_-20px_rgba(0,0,0,0.6)]"
      >
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-500/10 border border-amber-500/30 flex items-center justify-center shrink-0">
            <AlertTriangle className="w-5 h-5 text-amber-300" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-medium text-neutral-100">设置已保存，是否立即重启机器人？</h3>
            <p className="text-sm text-neutral-400 mt-1.5 leading-relaxed">
              重启大约需要 3 秒。期间机器人会暂时无响应；进行中的任务可在重启后通过任务列表查看。
            </p>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            className="inline-flex min-h-11 items-center gap-1.5 px-4 sm:h-9 sm:min-h-0 rounded-xl bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-sm disabled:opacity-50 transition-colors"
          >
            <X className="w-3.5 h-3.5" /> 暂不重启
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            className="inline-flex min-h-11 items-center gap-1.5 px-4 sm:h-9 sm:min-h-0 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 text-black text-sm font-medium disabled:opacity-50 transition-all"
          >
            {pending ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> 正在重启…
              </>
            ) : (
              <>
                <RotateCw className="w-3.5 h-3.5" /> 立即重启
              </>
            )}
          </button>
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
      <span className="text-xs text-neutral-300">{label}</span>
      <div className="relative">
        <input
          type={masked ? "password" : "text"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete="off"
          className={
            "w-full h-9 pr-20 pl-3 rounded-xl bg-[var(--bg-0)]/60 border border-white/10 focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 outline-none text-sm transition-colors " +
            (mono ? "font-mono" : "")
          }
        />
        {onToggleMask && (
          <button
            type="button"
            onClick={onToggleMask}
            className="absolute right-2 top-1/2 -translate-y-1/2 inline-flex items-center justify-center w-7 h-7 rounded-md bg-white/5 hover:bg-white/10 text-neutral-300 transition-colors"
            aria-label={masked ? "显示" : "隐藏"}
          >
            {masked ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
          </button>
        )}
      </div>
      <div className="flex items-start gap-2">
        <span className="text-[11px] text-neutral-500 leading-relaxed flex-1">{hint}</span>
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
      <button
        type="button"
        role="switch"
        aria-checked={on}
        onClick={() => onChange(!on)}
        className={
          "shrink-0 mt-0.5 w-11 h-6 rounded-full transition-colors relative " +
          (on
            ? "bg-[var(--color-lumen-amber)]"
            : "bg-white/10 border border-white/10")
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
        <p className="text-sm text-neutral-100">{label}</p>
        <p className="text-[11px] text-neutral-500 leading-relaxed">{hint}</p>
      </div>
    </div>
  );
}
