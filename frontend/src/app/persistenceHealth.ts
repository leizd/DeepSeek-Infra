import type { PersistenceFlushReport } from "./reloadBlockers";
import type { PersistenceFailureCode } from "./persistenceErrors";

export interface PersistenceFlusherError {
  code: PersistenceFailureCode;
  message: string;
}

export interface PersistenceHealthSnapshot {
  lastReport: PersistenceFlushReport | null;
  failedIds: readonly string[];
  lastFailureAt: number | null;
  lastSuccessRevision: Record<string, string>;
  lastErrors: Record<string, PersistenceFlusherError>;
  healthy: boolean;
}

const listeners = new Set<() => void>();

function emptySnapshot(): PersistenceHealthSnapshot {
  return {
    lastReport: null,
    failedIds: [],
    lastFailureAt: null,
    lastSuccessRevision: {},
    lastErrors: {},
    healthy: true,
  };
}

let snapshot: PersistenceHealthSnapshot = emptySnapshot();

/**
 * 记录每一次 checkpoint flush 的结果。失败的 Flusher 保留各自的
 * `lastSuccessRevision`（pagehide 场景下“最后成功 revision”不得被失败
 * 覆盖），只有后续成功且带 revision 的结果才会推进它。
 */
export function recordFlushReport(report: PersistenceFlushReport): void {
  const lastSuccessRevision = { ...snapshot.lastSuccessRevision };
  const lastErrors = { ...snapshot.lastErrors };
  let lastFailureAt = snapshot.lastFailureAt;
  for (const [id, result] of Object.entries(report.results)) {
    if (result.ok) {
      if (result.revision !== undefined) lastSuccessRevision[id] = result.revision;
      delete lastErrors[id];
    } else {
      lastErrors[id] = { code: result.code, message: result.message };
      lastFailureAt = Date.now();
    }
  }
  snapshot = {
    lastReport: report,
    failedIds: [...report.failedIds],
    lastFailureAt,
    lastSuccessRevision,
    lastErrors,
    healthy: report.ok,
  };
  listeners.forEach((listener) => listener());
}

export function getPersistenceHealthSnapshot(): PersistenceHealthSnapshot {
  return snapshot;
}

export function subscribePersistenceHealth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function resetPersistenceHealthForTests(): void {
  snapshot = emptySnapshot();
  listeners.forEach((listener) => listener());
}
