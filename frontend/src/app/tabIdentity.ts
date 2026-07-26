export const TAB_ID_STORAGE_KEY = "deepseek-infra.tab-id";

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
