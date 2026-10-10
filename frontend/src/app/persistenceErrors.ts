import type { PersistenceFlushResult } from "./reloadBlockers";

export type PersistenceFlushFailure = Extract<PersistenceFlushResult, { ok: false }>;
export type PersistenceFailureCode = PersistenceFlushFailure["code"];

const LEGACY_QUOTA_EXCEEDED_CODE = 22;
const QUOTA_ERROR_NAMES = new Set(["QuotaExceededError", "NS_ERROR_DOM_QUOTA_REACHED"]);

function describeError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error);
}

/**
 * 把 Web Storage 访问器抛出的异常归类为稳定的持久化失败码，供所有
 * checkpoint 来源（Composer 草稿、会话状态……）共用：
 * - QuotaExceededError（含旧数值 code 22 与 NS_ERROR_DOM_QUOTA_REACHED）→ quota-exceeded
 * - SecurityError（浏览器禁用了存储）→ storage-unavailable
 * - 其余一律 unknown，message 保留原始描述。
 */
export function classifyStorageError(error: unknown): PersistenceFlushFailure {
  const failure = (code: PersistenceFailureCode): PersistenceFlushFailure => ({
    ok: false,
    code,
    message: describeError(error),
  });
  if (typeof error === "object" && error !== null) {
    const { name, code } = error as { name?: unknown; code?: unknown };
    if ((typeof name === "string" && QUOTA_ERROR_NAMES.has(name)) || code === LEGACY_QUOTA_EXCEEDED_CODE) {
      return failure("quota-exceeded");
    }
    if (name === "SecurityError") {
      return failure("storage-unavailable");
    }
  }
  return failure("unknown");
}

/** 写入后回读核验不一致。 */
export function verificationFailure(message: string): PersistenceFlushFailure {
  return { ok: false, code: "verification-failed", message };
}

/** 存储整体不可用（sessionStorage 不存在，或访问器连读都抛错）。 */
export function storageUnavailableFailure(message: string): PersistenceFlushFailure {
  return { ok: false, code: "storage-unavailable", message };
}
