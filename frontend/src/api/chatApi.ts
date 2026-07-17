import { httpClient, type HttpClient } from "./httpClient";

export interface ChatRuntimeConfig {
  version: string;
  hasServerKey: boolean;
  hasSearch: boolean;
  defaultModel: string;
  models: readonly string[];
  modelRoutes: Readonly<Record<string, string>>;
}

export async function loadChatRuntimeConfig(client: HttpClient = httpClient): Promise<ChatRuntimeConfig> {
  const value = await client.json<Partial<ChatRuntimeConfig>>("/api/config");
  const models = Array.isArray(value.models) ? value.models.filter((model): model is string => typeof model === "string") : [];
  return {
    version: typeof value.version === "string" ? value.version : "4.0.3",
    hasServerKey: Boolean(value.hasServerKey),
    hasSearch: Boolean(value.hasSearch),
    defaultModel: typeof value.defaultModel === "string" ? value.defaultModel : models[0] ?? "deepseek-v4-pro",
    models: models.length ? models : ["deepseek-v4-flash", "deepseek-v4-pro"],
    modelRoutes: value.modelRoutes && typeof value.modelRoutes === "object" ? value.modelRoutes : {},
  };
}
