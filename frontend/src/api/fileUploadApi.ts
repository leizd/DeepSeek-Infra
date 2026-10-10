import type { JsonRecord } from "../domain/chat/types";

export const DEFAULT_UPLOAD_TIMEOUT_MS = 240_000;

export interface UploadLimits {
  fileMaxBytes: number;
  requestMaxBytes: number;
  maxFiles: number;
}

export const DEFAULT_UPLOAD_LIMITS: UploadLimits = {
  fileMaxBytes: 200_000_000,
  requestMaxBytes: 220_000_000,
  maxFiles: 8,
};

export interface UploadedFileError {
  name?: string;
  error?: string;
  code?: string;
  status?: number;
}

export interface FileUploadResult {
  files: JsonRecord[];
  errors: UploadedFileError[];
}

export interface FileUploadTask {
  readonly promise: Promise<FileUploadResult>;
  cancel(): void;
  readonly active: boolean;
}

export interface FileUploadOptions {
  files: readonly File[];
  url?: string;
  ocrEnabled?: boolean;
  apiKey?: string;
  authToken?: string;
  timeoutMs?: number;
  onProgress?: (percent: number) => void;
  onProcessing?: () => void;
  xhrFactory?: () => XMLHttpRequest;
}

export class FileUploadError extends Error {
  readonly code?: string;
  readonly status?: number;

  constructor(message: string, code?: string, status?: number) {
    super(message);
    this.name = "FileUploadError";
    this.code = code;
    this.status = status;
  }
}

function positiveTimeout(value: number | undefined): number {
  const timeout = Number(value ?? DEFAULT_UPLOAD_TIMEOUT_MS);
  return Number.isFinite(timeout) && timeout > 0 ? timeout : DEFAULT_UPLOAD_TIMEOUT_MS;
}

function abortError(): Error {
  try {
    return new DOMException("上传已取消", "AbortError");
  } catch {
    const error = new Error("上传已取消");
    error.name = "AbortError";
    return error;
  }
}

export function isAbortError(reason: unknown): boolean {
  return reason instanceof Error && reason.name === "AbortError";
}

export function createFileUploadTask(options: FileUploadOptions): FileUploadTask {
  const xhr = (options.xhrFactory ?? (() => new XMLHttpRequest()))();
  const formData = new FormData();
  for (const file of options.files) formData.append("files", file, file.name || "upload");
  if (options.ocrEnabled) formData.append("ocrEnabled", "1");
  if (options.apiKey) formData.append("apiKey", options.apiKey);

  let settled = false;
  let resolvePromise!: (value: FileUploadResult) => void;
  let rejectPromise!: (reason: unknown) => void;
  const promise = new Promise<FileUploadResult>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  const resolveOnce = (value: FileUploadResult) => {
    if (settled) return;
    settled = true;
    resolvePromise(value);
  };
  const rejectOnce = (reason: unknown) => {
    if (settled) return;
    settled = true;
    rejectPromise(reason);
  };

  xhr.open("POST", options.url ?? "/api/file-text");
  xhr.timeout = positiveTimeout(options.timeoutMs);
  if (options.authToken) xhr.setRequestHeader("Authorization", `Bearer ${options.authToken}`);

  xhr.upload.onprogress = (event) => {
    if (!event.lengthComputable || event.total <= 0) return;
    options.onProgress?.(Math.min(99, Math.round((event.loaded / event.total) * 100)));
  };
  xhr.upload.onload = () => {
    options.onProgress?.(100);
    options.onProcessing?.();
  };
  xhr.onload = () => {
    let data: JsonRecord = {};
    try {
      data = JSON.parse(xhr.responseText || "{}") as JsonRecord;
    } catch {
      rejectOnce(new FileUploadError("文件识别结果不是有效 JSON"));
      return;
    }
    if (xhr.status < 200 || xhr.status >= 300) {
      const message = typeof data.error === "string" && data.error ? data.error : `文件识别失败：${xhr.status}`;
      const code = typeof data.code === "string" ? data.code : undefined;
      rejectOnce(new FileUploadError(message, code, xhr.status));
      return;
    }
    const files = Array.isArray(data.files) ? (data.files as JsonRecord[]) : data.file && typeof data.file === "object" ? [data.file as JsonRecord] : [];
    const errors = Array.isArray(data.errors) ? (data.errors as UploadedFileError[]) : [];
    resolveOnce({ files, errors });
  };
  xhr.onerror = () => rejectOnce(new FileUploadError("上传失败，请检查网络"));
  xhr.ontimeout = () => rejectOnce(new FileUploadError("上传超时，请重试"));
  xhr.onabort = () => rejectOnce(abortError());
  xhr.send(formData);

  return {
    promise,
    cancel() {
      if (!settled) xhr.abort();
    },
    get active() {
      return !settled;
    },
  };
}
