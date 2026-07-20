import { httpClient, type HttpClient } from "./httpClient";
import { DEFAULT_UPLOAD_LIMITS, type UploadLimits } from "./fileUploadApi";

export interface ChatRuntimeConfig {
  version: string;
  hasServerKey: boolean;
  hasSearch: boolean;
  defaultModel: string;
  models: readonly string[];
  modelRoutes: Readonly<Record<string, string>>;
  uploadLimits: UploadLimits;
}

function normalizeUploadLimits(value: unknown): UploadLimits {
  const record = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const positive = (input: unknown, fallback: number) =>
    typeof input === "number" && Number.isFinite(input) && input > 0 ? input : fallback;
  return {
    fileMaxBytes: positive(record.fileMaxBytes, DEFAULT_UPLOAD_LIMITS.fileMaxBytes),
    requestMaxBytes: positive(record.requestMaxBytes, DEFAULT_UPLOAD_LIMITS.requestMaxBytes),
    maxFiles: positive(record.maxFiles, DEFAULT_UPLOAD_LIMITS.maxFiles),
  };
}

export async function loadChatRuntimeConfig(client: HttpClient = httpClient): Promise<ChatRuntimeConfig> {
  const value = await client.json<Partial<ChatRuntimeConfig>>("/api/config");
  const models = Array.isArray(value.models) ? value.models.filter((model): model is string => typeof model === "string") : [];
  return {
    version: typeof value.version === "string" ? value.version : "4.2.1",
    hasServerKey: Boolean(value.hasServerKey),
    hasSearch: Boolean(value.hasSearch),
    defaultModel: typeof value.defaultModel === "string" ? value.defaultModel : models[0] ?? "deepseek-v4-pro",
    models: models.length ? models : ["deepseek-v4-flash", "deepseek-v4-pro"],
    modelRoutes: value.modelRoutes && typeof value.modelRoutes === "object" ? value.modelRoutes : {},
    uploadLimits: normalizeUploadLimits(value.uploadLimits),
  };
}
