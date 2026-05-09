// 全站通用文案集中表（V1 设计语言统一, 2026-05-09）。
// 不引入 i18n 框架，纯 const map，便于将来切 next-intl。
// 微文案规范见 apps/web/DESIGN.md §5。
//
// 使用：import { copy } from "@/lib/copy";
//       <Button>{copy.action.save}</Button>

export const copy = {
  action: {
    save: "保存",
    cancel: "取消",
    confirm: "确认",
    delete: "删除",
    retry: "重试",
    back: "返回",
    close: "关闭",
    edit: "编辑",
    copy: "复制",
    export: "导出",
    import: "导入",
    continue: "继续",
    next: "下一步",
    prev: "上一步",
    submit: "提交",
    create: "新建",
    apply: "应用",
    reset: "重置",
  },
  state: {
    loading: "加载中",
    empty: "暂无",
    saved: "已保存",
    failed: "失败",
    success: "完成",
    noResult: "无结果",
    saving: "保存中",
    deleting: "删除中",
    submitting: "提交中",
    uploading: "上传中",
    copied: "已复制",
    deleted: "已删除",
  },
  error: {
    network: "网络异常",
    timeout: "请求超时",
    unauthorized: "登录已过期",
    invalid: "格式不正确",
    required: "此项必填",
    unknown: "操作失败",
    notFound: "资源不存在",
    forbidden: "无权访问",
  },
  hint: {
    unsaved: "有未保存改动",
    irreversible: "此操作不可撤销",
    confirm: "确认后立即生效",
    optional: "可选",
  },
} as const;

export type CopyAction = keyof typeof copy.action;
export type CopyState = keyof typeof copy.state;
export type CopyError = keyof typeof copy.error;
