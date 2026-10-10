export type FetchImplementation = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface HttpClientOptions {
  baseUrl?: string;
  fetchImpl?: FetchImplementation;
  getAuthToken?: () => string;
}

export interface ApiErrorPayload {
  error?: unknown;
  message?: unknown;
  code?: unknown;
  [key: string]: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly payload: ApiErrorPayload;

  constructor(message: string, status: number, payload: ApiErrorPayload = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    const nested = payload.error && typeof payload.error === "object" && !Array.isArray(payload.error)
      ? payload.error as { code?: unknown; message?: unknown }
      : undefined;
    this.code = typeof payload.code === "string" ? payload.code :
      typeof nested?.code === "string" ? nested.code : undefined;
    this.payload = payload;
  }
}

function errorMessage(payload: ApiErrorPayload, status: number): string {
  if (typeof payload.error === "string" && payload.error) return payload.error;
  if (payload.error && typeof payload.error === "object" && !Array.isArray(payload.error)) {
    const nested = payload.error as { message?: unknown };
    if (typeof nested.message === "string" && nested.message) return nested.message;
  }
  if (typeof payload.message === "string" && payload.message) return payload.message;
  return `Request failed (${status})`;
}

async function errorPayload(response: Response): Promise<ApiErrorPayload> {
  try {
    const value: unknown = await response.clone().json();
    return value && typeof value === "object" && !Array.isArray(value) ? (value as ApiErrorPayload) : {};
  } catch {
    return {};
  }
}

function joinUrl(baseUrl: string, path: string): string {
  if (!baseUrl || /^https?:\/\//i.test(path)) return path;
  return `${baseUrl.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

export class HttpClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchImplementation;
  private readonly getAuthToken: () => string;

  constructor(options: HttpClientOptions = {}) {
    this.baseUrl = options.baseUrl ?? "";
    this.fetchImpl = options.fetchImpl ?? fetch.bind(globalThis);
    this.getAuthToken = options.getAuthToken ?? (() => "");
  }

  async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (!headers.has("Accept")) headers.set("Accept", "application/json");
    const token = this.getAuthToken().trim();
    if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);

    const response = await this.fetchImpl(joinUrl(this.baseUrl, path), {
      credentials: "same-origin",
      ...init,
      headers,
    });
    if (!response.ok) {
      const payload = await errorPayload(response);
      throw new ApiError(errorMessage(payload, response.status), response.status, payload);
    }
    return response;
  }

  async json<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.request(path, init);
    return (await response.json()) as T;
  }

  async postJson<T>(path: string, body: unknown, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    return this.json<T>(path, {
      ...init,
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
  }
}

export const httpClient = new HttpClient();
