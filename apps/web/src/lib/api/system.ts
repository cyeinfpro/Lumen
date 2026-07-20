import { apiFetch } from "./http";
import type {
  AdminContextHealthOut,
  AdminModelsOut,
  ApiSupplierProbeOut,
  ApiSupplierTemplateIn,
  ApiSupplierTemplateListOut,
  ApiSupplierTemplateOut,
  ApiSupplierTemplatePublicListOut,
  ByokSettingsOut,
  ByokSettingsPatchIn,
  ProviderItemIn,
  ProviderItemOut,
  ProviderProxyIn,
  ProvidersOut,
  ProvidersProbeOut,
  ProviderStatsOut,
  SystemSettingsOut,
  TelegramLinkCodeOut,
  UserApiCredentialListOut,
  UserApiCredentialOut,
  VideoProvidersOut,
  VideoProvidersUpdateIn,
} from "../types";

// ——— Admin: system settings ———

const SYSTEM_SETTINGS_BASE = "/admin/settings";

export function getSystemSettings(): Promise<SystemSettingsOut> {
  return apiFetch<SystemSettingsOut>(SYSTEM_SETTINGS_BASE);
}

export function updateSystemSettings(
  items: { key: string; value: string }[],
): Promise<SystemSettingsOut> {
  return apiFetch<SystemSettingsOut>(SYSTEM_SETTINGS_BASE, {
    method: "PUT",
    body: JSON.stringify({ items }),
  });
}

export function getAdminModels(): Promise<AdminModelsOut> {
  return apiFetch<AdminModelsOut>("/admin/models");
}

export function getAdminContextHealth(): Promise<AdminContextHealthOut> {
  return apiFetch<AdminContextHealthOut>("/admin/context/health");
}

// ——— Admin: providers ———

const PROVIDERS_BASE = "/admin/providers";

export function getProviders(): Promise<ProvidersOut> {
  return apiFetch<ProvidersOut>(PROVIDERS_BASE);
}

export async function updateProviders(
  payload:
    ProviderItemIn[] | { items: ProviderItemIn[]; proxies?: ProviderProxyIn[] },
): Promise<ProvidersOut> {
  const body = Array.isArray(payload)
    ? { items: payload, proxies: [] }
    : { items: payload.items, proxies: payload.proxies ?? [] };
  return apiFetch<ProvidersOut>(PROVIDERS_BASE, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function patchProviderEnabled(
  name: string,
  enabled: boolean,
): Promise<ProviderItemOut> {
  return apiFetch<ProviderItemOut>(
    `${PROVIDERS_BASE}/${encodeURIComponent(name)}/enabled`,
    {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    },
  );
}

export function probeProviders(names?: string[]): Promise<ProvidersProbeOut> {
  return apiFetch<ProvidersProbeOut>(`${PROVIDERS_BASE}/probe`, {
    method: "POST",
    ...(names ? { body: JSON.stringify({ names }) } : {}),
  });
}

export function getProviderStats(): Promise<ProviderStatsOut> {
  return apiFetch<ProviderStatsOut>(`${PROVIDERS_BASE}/stats`);
}

export function getVideoProviders(): Promise<VideoProvidersOut> {
  return apiFetch<VideoProvidersOut>(`${PROVIDERS_BASE}/video`);
}

export function updateVideoProviders(
  body: VideoProvidersUpdateIn,
): Promise<VideoProvidersOut> {
  return apiFetch<VideoProvidersOut>(`${PROVIDERS_BASE}/video`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ——— BYOK ———

export function getByokSettings(): Promise<ByokSettingsOut> {
  return apiFetch<ByokSettingsOut>("/admin/byok-settings");
}

export function patchByokSettings(
  body: ByokSettingsPatchIn,
): Promise<ByokSettingsOut> {
  return apiFetch<ByokSettingsOut>("/admin/byok-settings", {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function listApiSuppliers(): Promise<ApiSupplierTemplateListOut> {
  return apiFetch<ApiSupplierTemplateListOut>("/admin/api-suppliers");
}

export function createApiSupplier(
  body: ApiSupplierTemplateIn,
): Promise<ApiSupplierTemplateOut> {
  return apiFetch<ApiSupplierTemplateOut>("/admin/api-suppliers", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchApiSupplier(
  id: string,
  body: Partial<ApiSupplierTemplateIn>,
): Promise<ApiSupplierTemplateOut> {
  return apiFetch<ApiSupplierTemplateOut>(`/admin/api-suppliers/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function probeApiSupplier(
  id: string,
  api_key: string,
): Promise<ApiSupplierProbeOut> {
  return apiFetch<ApiSupplierProbeOut>(`/admin/api-suppliers/${id}/probe`, {
    method: "POST",
    body: JSON.stringify({ api_key }),
  });
}

export function listMyApiCredentials(): Promise<UserApiCredentialListOut> {
  return apiFetch<UserApiCredentialListOut>("/me/api-credentials");
}

export function listBindableApiSuppliers(): Promise<ApiSupplierTemplatePublicListOut> {
  return apiFetch<ApiSupplierTemplatePublicListOut>(
    "/me/api-credentials/suppliers",
  );
}

export function putMyApiCredential(
  supplier_id: string,
  api_key: string,
): Promise<UserApiCredentialOut> {
  return apiFetch<UserApiCredentialOut>(`/me/api-credentials/${supplier_id}`, {
    method: "PUT",
    body: JSON.stringify({ api_key }),
  });
}

export function probeMyApiCredential(
  credential_id: string,
): Promise<UserApiCredentialOut> {
  return apiFetch<UserApiCredentialOut>(
    `/me/api-credentials/${credential_id}/probe`,
    { method: "POST" },
  );
}

export function revokeMyApiCredential(
  credential_id: string,
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/me/api-credentials/${credential_id}`, {
    method: "DELETE",
  });
}

export function createTelegramLinkCode(): Promise<TelegramLinkCodeOut> {
  return apiFetch<TelegramLinkCodeOut>("/me/telegram/link-code", {
    method: "POST",
  });
}
