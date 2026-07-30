export const DEFAULT_LOCAL_ROOT = "/var/lib/lumen-data";

export type StorageBackend = "local" | "smb";

export interface StorageFormState {
  backend: StorageBackend;
  localRoot: string;
  host: string;
  port: string;
  share: string;
  subpath: string;
  username: string;
  password: string;
}
