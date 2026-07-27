export const TAB_ID_STORAGE_KEY = "deepseek-infra.tab-id";
export const TAB_CONTINUITY_STORAGE_KEY = TAB_ID_STORAGE_KEY;

const TAB_ID_PATTERN = /^[0-9a-f]{8}$/;

export interface TabStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function defaultTabStorage(): TabStorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function randomTabId(): string {
  return Math.floor(Math.random() * 0x1_0000_0000).toString(16).padStart(8, "0");
}

let inMemoryTabId: string | null = null;

function randomUuid(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === "function") cryptoApi.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * 标签页连续性与写入者身份刻意拆分：
 * - tabContinuityId 只用于 sessionStorage 中的 UI 连续性（例如当前会话）；
 * - writerSessionId 标识一个真实 Document 生命周期，revision / lease / capsule 使用它；
 * - documentInstanceId 只用于 BroadcastChannel 身份声明与重复实例检测。
 */
export interface ReplicaIdentity {
  tabContinuityId: string;
  writerSessionId: string;
  documentInstanceId: string;
}

export interface ReplicaIdentityOverrides {
  writerSessionId?: string;
  documentInstanceId?: string;
}

export function createReplicaIdentity(
  storage: TabStorageLike | null = defaultTabStorage(),
  overrides: ReplicaIdentityOverrides = {},
): ReplicaIdentity {
  return {
    tabContinuityId: getTabId(storage),
    writerSessionId: overrides.writerSessionId ?? randomUuid(),
    documentInstanceId: overrides.documentInstanceId ?? randomUuid(),
  };
}

/**
 * 本标签页的稳定标识：优先读写 sessionStorage（同源标签页各自独立），
 * 存储不可用时退化为进程内记忆值，保证同一会话周期内 revision 的 writerId 一致。
 */
export function getTabId(storage: TabStorageLike | null = defaultTabStorage()): string {
  try {
    const existing = storage?.getItem(TAB_ID_STORAGE_KEY);
    if (existing && TAB_ID_PATTERN.test(existing)) return existing;
  } catch {
    // 读取失败按无存储处理，落到记忆值。
  }
  if (!inMemoryTabId) inMemoryTabId = randomTabId();
  try {
    storage?.setItem(TAB_ID_STORAGE_KEY, inMemoryTabId);
  } catch {
    // 写入失败无害：记忆值已保证本次会话周期内稳定。
  }
  return inMemoryTabId;
}
