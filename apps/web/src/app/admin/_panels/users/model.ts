import type { AdminUserOut } from "@/lib/types";

export type UserRoleFilter = "all" | "admin" | "member";

export function userMatchesFilters(
  user: AdminUserOut,
  roleFilter: UserRoleFilter,
  normalizedSearch: string,
): boolean {
  const matchesRole = roleFilter === "all" || user.role === roleFilter;
  const matchesSearch =
    normalizedSearch.length === 0 ||
    user.email.toLowerCase().includes(normalizedSearch) ||
    (user.display_name ?? "").toLowerCase().includes(normalizedSearch);
  return matchesRole && matchesSearch;
}

export function userRoleFilterLabel(role: UserRoleFilter): string {
  const labels: Record<UserRoleFilter, string> = {
    all: "全部",
    admin: "管理员",
    member: "成员",
  };
  return labels[role];
}

export function emptyUsersDescription(rowCount: number): string {
  return rowCount === 0 ? "注册的用户会出现在这里" : "试试切换角色或换个关键词";
}

export function emptyUsersTitle(rowCount: number): string {
  return rowCount === 0 ? "暂无用户" : "没有匹配结果";
}
