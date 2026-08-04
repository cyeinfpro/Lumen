"use client";

import {
  useId,
  useRef,
  type FormEvent,
} from "react";
import {
  Copy,
  Download,
  Gift,
  MoreHorizontal,
  RefreshCw,
  Search,
  Slash,
  X,
} from "lucide-react";

import {
  adminRedemptionBatchCsvUrl,
  adminRedemptionBatchTxtUrl,
} from "@/lib/apiClient";
import type {
  AdminRedemptionCodeOut,
  AdminRedemptionUsageOut,
} from "@/lib/types";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { Button, Card } from "@/components/ui/primitives";
import { useModalLayer } from "@/components/ui/primitives/mobile";
import { formatRmb } from "@/lib/money";
import { ErrorBlock } from "../_components/AdminFeedback";

export type CodeStatus =
  | "all"
  | "active"
  | "revoked"
  | "expired"
  | "exhausted";
export type WalletMode = "wallet" | "byok" | "all";
export type AccountMode = "wallet" | "byok";
export type ResidualMode = "freeze" | "zero";

export const STATUS_LABEL: Record<CodeStatus, string> = {
  all: "全部",
  active: "可兑",
  exhausted: "已兑完",
  revoked: "撤销",
  expired: "过期",
};

export const TX_KIND_LABEL: Record<string, string> = {
  all: "全部",
  hold: "预扣",
  settle: "结算",
  release: "释放",
  charge: "扣费",
  charge_completion: "扣费",
  topup_redeem: "兑换充值",
  adjust_admin: "管理员调账",
};

export interface NewCodesModalState {
  batchId: string;
  amountRmb: string;
  downloadToken: string;
  codes: string[];
}

function formatMoney(value?: string | null): string {
  return formatRmb(value);
}

export function CodeBatchForm({
  amount,
  count,
  maxRedemptions,
  note,
  expiresAt,
  totalValue,
  isCreating,
  onAmountChange,
  onCountChange,
  onMaxRedemptionsChange,
  onNoteChange,
  onExpiresAtChange,
  onCreate,
}: {
  amount: string;
  count: string;
  maxRedemptions: string;
  note: string;
  expiresAt: string;
  totalValue: number;
  isCreating: boolean;
  onAmountChange: (value: string) => void;
  onCountChange: (value: string) => void;
  onMaxRedemptionsChange: (value: string) => void;
  onNoteChange: (value: string) => void;
  onExpiresAtChange: (value: string) => void;
  onCreate: () => void;
}) {
  return (
    <Card variant="subtle" padding="lg" className="space-y-4">
      <div className="flex items-center gap-2 type-card-title">
        <Gift className="h-4 w-4" />
        批量发码
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <label className="space-y-1.5">
          <span className="block min-h-4 type-caption text-[var(--fg-2)]">
            面额 (¥/张)
          </span>
          <input
            value={amount}
            onChange={(event) => onAmountChange(event.target.value)}
            inputMode="decimal"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
        <label className="space-y-1.5">
          <span className="block min-h-4 type-caption text-[var(--fg-2)]">
            数量
          </span>
          <input
            value={count}
            onChange={(event) => onCountChange(event.target.value)}
            inputMode="numeric"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
        <label className="space-y-1.5">
          <span className="block min-h-4 type-caption text-[var(--fg-2)]">
            每码最大兑换次数
          </span>
          <input
            value={maxRedemptions}
            onChange={(event) => onMaxRedemptionsChange(event.target.value)}
            inputMode="numeric"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
        <label className="space-y-1.5">
          <span className="block min-h-4 type-caption text-[var(--fg-2)]">
            有效期
          </span>
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => onExpiresAtChange(event.target.value)}
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
        </label>
      </div>
      <input
        value={note}
        onChange={(event) => onNoteChange(event.target.value)}
        placeholder="备注"
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm md:max-w-[480px]"
      />
      <div className="flex flex-col-reverse items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="type-body-sm text-[var(--fg-2)]">
          本批次总价值 ¥{totalValue.toFixed(2)}
        </p>
        <Button
          variant="primary"
          size="md"
          className="w-full sm:w-auto"
          onClick={onCreate}
          loading={isCreating}
        >
          创建兑换码
        </Button>
      </div>
    </Card>
  );
}

interface RedemptionCodeActions {
  onCopy: (text: string, label: string) => void;
  onRedownloadBatch: (batchId: string) => void;
  onRevokeCode: (codeId: string) => void;
  onRevokeBatch: (batchId: string) => void;
  onOpenUsage: (codeId: string) => void;
  onOpenMobileActions: (code: AdminRedemptionCodeOut) => void;
  redownloadPending: boolean;
  revokeBatchPending: boolean;
}

function RedemptionCodeRow({
  code,
  actions,
}: {
  code: AdminRedemptionCodeOut;
  actions: RedemptionCodeActions;
}) {
  const batchId = code.batch_id;
  const active = !code.revoked_at;

  return (
    <tr className="border-b border-[var(--border-subtle)]">
      <td data-label="前缀" className="px-4 py-2 font-mono">
        {code.code_prefix}
      </td>
      <td data-label="面额" className="px-4 py-2 tabular-nums">
        ¥{formatMoney(code.amount.rmb)}
      </td>
      <td data-label="兑换" className="px-4 py-2 tabular-nums">
        {code.redeemed_count} / {code.max_redemptions}
        <span className="ml-1 text-[var(--fg-3)]">可用 {code.usable_count}</span>
      </td>
      <td data-label="状态" className="px-4 py-2">
        {STATUS_LABEL[code.status] ?? code.status}
      </td>
      <td data-label="批次" className="px-4 py-2">
        <button
          type="button"
          onClick={() => {
            if (batchId) actions.onCopy(batchId, "批次已复制");
          }}
          className="max-w-full truncate font-mono text-xs text-[var(--fg-1)] hover:text-[var(--fg-0)] md:max-w-[160px]"
        >
          {batchId ?? "-"}
        </button>
      </td>
      <td data-label="备注" className="px-4 py-2 text-[var(--fg-2)]">
        <span className="line-clamp-2 md:block md:max-w-[180px] md:truncate">
          {code.note ?? "-"}
        </span>
      </td>
      <td data-actions="true" className="px-4 py-2 text-right">
        <div className="hidden justify-end md:flex md:flex-wrap md:gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => actions.onCopy(code.code_prefix, "前缀已复制")}
            leftIcon={<Copy className="h-3.5 w-3.5" />}
          >
            前缀
          </Button>
          {batchId && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => actions.onRedownloadBatch(batchId)}
              loading={actions.redownloadPending}
            >
              重新查看
            </Button>
          )}
          {active && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => actions.onRevokeCode(code.id)}
              leftIcon={<Slash className="h-3.5 w-3.5" />}
            >
              撤销
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => actions.onOpenUsage(code.id)}
          >
            记录
          </Button>
          {active && batchId && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => actions.onRevokeBatch(batchId)}
              loading={actions.revokeBatchPending}
            >
              撤销批次
            </Button>
          )}
        </div>
        <div className="md:hidden">
          <Button
            variant="outline"
            size="sm"
            onClick={() => actions.onOpenMobileActions(code)}
            leftIcon={<MoreHorizontal className="h-3.5 w-3.5" />}
          >
            更多
          </Button>
        </div>
      </td>
    </tr>
  );
}

function RedemptionCodesContent({
  items,
  isLoading,
  isError,
  errorMessage,
  nextCursor,
  onRetry,
  onNextPage,
  actions,
}: {
  items: AdminRedemptionCodeOut[];
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  nextCursor?: string | null;
  onRetry: () => void;
  onNextPage: (cursor: string) => void;
  actions: RedemptionCodeActions;
}) {
  if (isError) {
    return (
      <div role="alert" className="p-4">
        <ErrorBlock message={errorMessage} onRetry={onRetry} />
      </div>
    );
  }

  return (
    <>
      <div className="data-stack-on-mobile md:overflow-x-auto">
        <table className="w-full text-sm md:min-w-[980px]">
          <thead className="text-left text-[var(--fg-2)]">
            <tr className="border-b border-[var(--border-subtle)]">
              <th className="px-4 py-2">前缀</th>
              <th className="px-4 py-2">面额</th>
              <th className="px-4 py-2">兑换</th>
              <th className="px-4 py-2">状态</th>
              <th className="px-4 py-2">批次</th>
              <th className="px-4 py-2">备注</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {items.map((code) => (
              <RedemptionCodeRow key={code.id} code={code} actions={actions} />
            ))}
            {!isLoading && items.length === 0 && (
              <tr>
                <td
                  className="px-4 py-8 text-center text-[var(--fg-2)]"
                  colSpan={7}
                >
                  暂无兑换码
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="flex justify-end border-t border-[var(--border-subtle)] px-4 py-3">
        <Button
          variant="outline"
          size="sm"
          disabled={!nextCursor}
          onClick={() => {
            if (nextCursor) onNextPage(nextCursor);
          }}
        >
          下一页
        </Button>
      </div>
    </>
  );
}

export function RedemptionCodesCard({
  status,
  qInput,
  items,
  isLoading,
  isError,
  errorMessage,
  nextCursor,
  onStatusChange,
  onQInputChange,
  onSearch,
  onRefresh,
  onRetry,
  onNextPage,
  actions,
}: {
  status: CodeStatus;
  qInput: string;
  items: AdminRedemptionCodeOut[];
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  nextCursor?: string | null;
  onStatusChange: (status: CodeStatus) => void;
  onQInputChange: (value: string) => void;
  onSearch: () => void;
  onRefresh: () => void;
  onRetry: () => void;
  onNextPage: (cursor: string) => void;
  actions: RedemptionCodeActions;
}) {
  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSearch();
  };

  return (
    <Card variant="subtle" padding="none" className="overflow-hidden">
      <div className="space-y-3 border-b border-[var(--border-subtle)] px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="type-card-title">兑换码</p>
          <Button
            variant="ghost"
            size="sm"
            onClick={onRefresh}
            leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            刷新
          </Button>
        </div>
        <div className="scrollbar-thin flex flex-nowrap items-center gap-2 overflow-x-auto overscroll-x-contain">
          {(Object.keys(STATUS_LABEL) as CodeStatus[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => onStatusChange(item)}
              className={[
                "shrink-0 rounded-full border px-3 py-1 text-xs",
                status === item
                  ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
                  : "border-[var(--border)] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
              ].join(" ")}
            >
              {STATUS_LABEL[item]}
            </button>
          ))}
        </div>
        <form
          onSubmit={submitSearch}
          className="grid gap-2 md:grid-cols-[1fr_auto]"
        >
          <input
            value={qInput}
            onChange={(event) => onQInputChange(event.target.value)}
            placeholder="搜索前缀或 batch id"
            className="h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm"
          />
          <Button
            variant="outline"
            size="md"
            type="submit"
            fullWidth
            leftIcon={<Search className="h-3.5 w-3.5" />}
          >
            搜索
          </Button>
        </form>
      </div>
      <RedemptionCodesContent
        items={items}
        isLoading={isLoading}
        isError={isError}
        errorMessage={errorMessage}
        nextCursor={nextCursor}
        onRetry={onRetry}
        onNextPage={onNextPage}
        actions={actions}
      />
    </Card>
  );
}

function RedemptionUsageContent({
  items,
  isLoading,
  isError,
  errorMessage,
  onRetry,
}: {
  items: AdminRedemptionUsageOut[];
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
}) {
  if (isLoading) {
    return (
      <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
        加载中
      </div>
    );
  }
  if (isError) {
    return (
      <div role="alert" className="p-4">
        <ErrorBlock message={errorMessage} onRetry={onRetry} />
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-sm text-[var(--fg-2)]">
        暂无兑换记录
      </div>
    );
  }

  return (
    <div className="data-stack-on-mobile md:overflow-x-auto">
      <table className="w-full text-sm md:min-w-[720px]">
        <thead className="text-left text-[var(--fg-2)]">
          <tr className="border-b border-[var(--border-subtle)]">
            <th className="px-4 py-2">用户</th>
            <th className="px-4 py-2">面额</th>
            <th className="px-4 py-2">流水</th>
            <th className="px-4 py-2">IP Hash</th>
            <th className="px-4 py-2">时间</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} className="border-b border-[var(--border-subtle)]">
              <td data-label="用户" className="px-4 py-2">
                <span className="block min-w-0 truncate">
                  {item.user_email ?? item.user_id}
                </span>
                <span className="block min-w-0 truncate font-mono text-xs text-[var(--fg-3)]">
                  {item.user_id}
                </span>
              </td>
              <td data-label="面额" className="px-4 py-2 tabular-nums">
                ¥{formatMoney(item.amount.rmb)}
              </td>
              <td
                data-label="流水"
                className="truncate px-4 py-2 font-mono text-xs"
              >
                {item.wallet_tx_id}
              </td>
              <td
                data-label="IP Hash"
                className="truncate px-4 py-2 font-mono text-xs"
              >
                {item.ip_hash ?? "-"}
              </td>
              <td data-label="时间" className="px-4 py-2">
                {new Date(item.redeemed_at).toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RedemptionUsageCard({
  open,
  items,
  isLoading,
  isError,
  errorMessage,
  onRetry,
  onClose,
}: {
  open: boolean;
  items: AdminRedemptionUsageOut[];
  isLoading: boolean;
  isError: boolean;
  errorMessage: string;
  onRetry: () => void;
  onClose: () => void;
}) {
  if (!open) return null;

  return (
    <Card variant="subtle" padding="none" className="overflow-hidden">
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-3">
        <p className="type-card-title">兑换记录</p>
        <Button variant="ghost" size="sm" onClick={onClose}>
          关闭
        </Button>
      </div>
      <RedemptionUsageContent
        items={items}
        isLoading={isLoading}
        isError={isError}
        errorMessage={errorMessage}
        onRetry={onRetry}
      />
    </Card>
  );
}

export function NewCodesModal({
  state,
  onClose,
  onCopy,
}: {
  state: NewCodesModalState;
  onClose: () => void;
  onCopy: (text: string, label: string) => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  useBodyScrollLock(true);
  const onDialogKeyDown = useModalLayer({
    open: true,
    rootRef: dialogRef,
    onClose,
  });
  const allCodes = state.codes.join("\n");

  return (
    <div
      data-lumen-modal-layer
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--surface-scrim)] sm:items-center"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
        className="surface-dialog mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] min-h-0 w-full max-w-3xl flex-col overflow-hidden rounded-t-[var(--radius-sheet)] text-[var(--fg-0)] focus:outline-none sm:h-auto sm:rounded-[var(--radius-dialog)]"
      >
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <h2 id={titleId} className="type-card-title">
              已生成 {state.codes.length} 张兑换码
            </h2>
            <p className="truncate type-caption text-[var(--fg-2)]">
              批次 {state.batchId}
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 border-b border-[var(--border)] px-5 py-3">
          <Button
            variant="primary"
            size="sm"
            onClick={() => onCopy(allCodes, "全部兑换码已复制")}
            leftIcon={<Copy className="h-3.5 w-3.5" />}
          >
            复制全部
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              window.open(
                adminRedemptionBatchCsvUrl(
                  state.batchId,
                  state.downloadToken,
                ),
                "_blank",
                "noopener,noreferrer",
              )
            }
            leftIcon={<Download className="h-3.5 w-3.5" />}
          >
            下载 CSV
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              window.open(
                adminRedemptionBatchTxtUrl(
                  state.batchId,
                  state.downloadToken,
                ),
                "_blank",
                "noopener,noreferrer",
              )
            }
            leftIcon={<Download className="h-3.5 w-3.5" />}
          >
            下载 TXT
          </Button>
        </div>
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain p-5">
          <div className="space-y-2">
            {state.codes.map((code) => (
              <div
                key={code}
                className="grid grid-cols-[1fr_auto] items-center gap-3 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-2"
              >
                <code className="min-w-0 select-all overflow-x-auto whitespace-nowrap font-mono text-sm scrollbar-thin">
                  {code}
                </code>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onCopy(code, "兑换码已复制")}
                >
                  复制
                </Button>
              </div>
            ))}
          </div>
        </div>
        <footer
          id={descriptionId}
          className="mobile-dialog-footer flex shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/72 px-5 pt-3 text-xs text-[var(--fg-2)]"
        >
          关闭后，5 分钟内可在列表里点“重新查看”再次取回明文；超过窗口后明文不会再被保存。
        </footer>
      </div>
    </div>
  );
}

export {
  WalletDetailSection,
  WalletList,
  WalletSearchForm,
  WalletsCard,
} from "./RedemptionWalletViews";
