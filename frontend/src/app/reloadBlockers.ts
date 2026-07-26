export interface ReloadBlocker {
  id: string;
  label: string;
  kind: "transient" | "unsaved";
  active: boolean;
}

export type PersistenceFlushResult =
  | { ok: true; revision?: string }
  | {
      ok: false;
      code:
        | "quota-exceeded"
        | "storage-unavailable"
        | "verification-failed"
        | "write-conflict"
        | "storage-pressure"
        | "unknown";
      message: string;
    };

export interface PersistenceFlushReport {
  ok: boolean;
  results: Record<string, PersistenceFlushResult>;
  failedIds: string[];
}

export type ReloadFlusher = () => PersistenceFlushResult | void;

const blockers = new Map<string, ReloadBlocker>();
const flushers = new Map<string, ReloadFlusher>();
const flusherFailureLabels = new Map<string, string>();
const listeners = new Set<() => void>();
let snapshot: readonly ReloadBlocker[] = [];

function publish(): void {
  snapshot = [...blockers.values()]
    .filter((blocker) => blocker.active)
    .sort((left, right) => left.id.localeCompare(right.id));
  listeners.forEach((listener) => listener());
}

export function setReloadBlocker(blocker: ReloadBlocker): void {
  const current = blockers.get(blocker.id);
  if (!blocker.active) {
    if (blockers.delete(blocker.id)) publish();
    return;
  }
  if (
    current?.label === blocker.label
    && current.kind === blocker.kind
    && current.active === blocker.active
  ) {
    return;
  }
  blockers.set(blocker.id, blocker);
  publish();
}

export function clearReloadBlocker(id: string): void {
  if (blockers.delete(id)) publish();
}

export function getReloadBlockerSnapshot(): readonly ReloadBlocker[] {
  return snapshot;
}

export function subscribeReloadBlockers(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function registerReloadFlusher(
  id: string,
  flusher: ReloadFlusher,
  options?: { failureLabel?: string },
): () => void {
  flushers.set(id, flusher);
  if (options?.failureLabel) {
    flusherFailureLabels.set(id, options.failureLabel);
  } else {
    flusherFailureLabels.delete(id);
  }
  return () => {
    if (flushers.get(id) === flusher) {
      flushers.delete(id);
      flusherFailureLabels.delete(id);
    }
  };
}

export function getReloadFlusherFailureLabel(id: string): string | undefined {
  return flusherFailureLabels.get(id);
}

function runFlusher(flusher: ReloadFlusher): PersistenceFlushResult {
  try {
    const result = flusher();
    if (result === undefined || result === null) return { ok: true };
    return result;
  } catch (reason) {
    return {
      ok: false,
      code: "unknown",
      message: reason instanceof Error && reason.message ? reason.message : String(reason),
    };
  }
}

export function flushReloadPersistence(): PersistenceFlushReport {
  const results: Record<string, PersistenceFlushResult> = {};
  const failedIds: string[] = [];
  for (const [id, flusher] of flushers) {
    const result = runFlusher(flusher);
    results[id] = result;
    if (!result.ok) failedIds.push(id);
  }
  return { ok: failedIds.length === 0, results, failedIds };
}

export function retryFailedFlushers(failedIds: string[]): PersistenceFlushReport {
  const results: Record<string, PersistenceFlushResult> = {};
  const stillFailed: string[] = [];
  for (const id of failedIds) {
    const flusher = flushers.get(id);
    if (!flusher) continue;
    const result = runFlusher(flusher);
    results[id] = result;
    if (!result.ok) stillFailed.push(id);
  }
  return { ok: stillFailed.length === 0, results, failedIds: stillFailed };
}

export function resetReloadCoordinationForTests(): void {
  blockers.clear();
  flushers.clear();
  flusherFailureLabels.clear();
  publish();
}
