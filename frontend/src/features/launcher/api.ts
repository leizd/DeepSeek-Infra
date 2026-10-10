export interface LauncherConfig {
  deepseek_api_key: string;
  tavily_api_key: string;
  host: string;
  port: number;
  allow_lan: boolean;
  ocr_enabled: boolean;
  auth_disabled: boolean;
}

export interface LauncherState {
  status: "stopped" | "starting" | "running" | "stopping" | "failed";
  computer_url: string;
  phone_urls: string[];
  error: string;
  logs: string[];
}

export const defaultLauncherConfig: LauncherConfig = {
  deepseek_api_key: "", tavily_api_key: "", host: "127.0.0.1", port: 8000,
  allow_lan: false, ocr_enabled: false, auth_disabled: false,
};

export class LauncherError extends Error {
  constructor(message: string, readonly code: string) { super(message); }
}

export async function launcherRequest<T>(operation: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/launcher/v1/${operation}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin", cache: "no-store", signal,
    headers: body === undefined ? undefined : { "Content-Type": "application/json", "X-DeepSeek-Launcher": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json() as T & { error?: string; code?: string };
  if (!response.ok) throw new LauncherError(result.error || "启动器操作失败，请重试", result.code || "LAUNCHER_ERROR");
  return result;
}
