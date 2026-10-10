import { composerDraftStorageKey, type ComposerDraftScope } from "./composerDraftPersistence";

/**
 * 草稿的内存副本。sessionStorage 写失败（配额满、存储被禁用）时，
 * 文本仍以 dirty 状态留在内存里：切换到别的会话/项目再切回来能
 * 原样恢复，并由防抖保存自动重试落盘。
 */
export interface ComposerDraftMemoryEntry {
  text: string;
  updatedAt: number;
  /** 最后一次成功写入存储的 revision（String(updatedAt)），从未成功为 null。 */
  persistedRevision: string | null;
  /** true 表示内存里的文本比存储新（有写入未成功或尚未落盘）。 */
  dirty: boolean;
}

const entries = new Map<string, ComposerDraftMemoryEntry>();

/**
 * 任何输入先同步落内存，再尝试写存储——存储失败也不丢文本。
 * 已落盘且文本未变化的重复调用是 no-op。
 */
export function rememberDraft(scope: ComposerDraftScope, draft: { text: string; updatedAt?: number }): void {
  const key = composerDraftStorageKey(scope);
  const existing = entries.get(key);
  if (existing && !existing.dirty && existing.text === draft.text) return;
  entries.set(key, {
    text: draft.text,
    updatedAt: draft.updatedAt ?? Date.now(),
    persistedRevision: existing?.persistedRevision ?? null,
    dirty: true,
  });
}

/**
 * 存储写入成功后调用，把条目标记为已落盘。调用方需保证写入的文本
 * 与当前内存文本一致（更旧的定时器不得把更新的文本误标为已落盘）。
 */
export function markPersisted(scope: ComposerDraftScope, revision: string): void {
  const entry = entries.get(composerDraftStorageKey(scope));
  if (!entry) return;
  entry.persistedRevision = revision;
  entry.dirty = false;
}

export function recallDraft(scope: ComposerDraftScope): ComposerDraftMemoryEntry | null {
  const entry = entries.get(composerDraftStorageKey(scope));
  return entry ? { ...entry } : null;
}

/** 清空成功（空文本 / 提交完成）后遗忘该作用域的内存条目。 */
export function forgetDraft(scope: ComposerDraftScope): void {
  entries.delete(composerDraftStorageKey(scope));
}

export function resetComposerDraftRepositoryForTests(): void {
  entries.clear();
}
