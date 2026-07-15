export type VolcanoAssetCredentialInput = {
  renamed: boolean;
  accessKeyId: string;
  secretAccessKey: string;
  storedAccessKeyIdHint?: string | null;
  storedSecretAccessKeyHint?: string | null;
  assetManagementReady?: boolean;
};

export type VolcanoAssetCredentialRule = {
  started: boolean;
  complete: boolean;
  replacementRequired: boolean;
  error: "incomplete" | "rename_replacement" | null;
};

export function evaluateVolcanoAssetCredentials({
  renamed,
  accessKeyId,
  secretAccessKey,
  storedAccessKeyIdHint,
  storedSecretAccessKeyHint,
  assetManagementReady,
}: VolcanoAssetCredentialInput): VolcanoAssetCredentialRule {
  const started = Boolean(accessKeyId.trim() || secretAccessKey.trim());
  const complete = Boolean(accessKeyId.trim() && secretAccessKey.trim());
  const hadStoredConfiguration = Boolean(
    assetManagementReady ||
      storedAccessKeyIdHint?.trim() ||
      storedSecretAccessKeyHint?.trim(),
  );
  const replacementRequired = renamed && hadStoredConfiguration;
  const error = !complete
    ? replacementRequired
      ? "rename_replacement"
      : started
        ? "incomplete"
        : null
    : null;
  return { started, complete, replacementRequired, error };
}
