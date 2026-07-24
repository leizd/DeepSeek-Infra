export interface ReloadBlocker {
  id: string;
  label: string;
  kind: "transient" | "unsaved";
  active: boolean;
}

export type ReloadFlusher = () => void;

const blockers = new Map<string, ReloadBlocker>();
const flushers = new Map<string, ReloadFlusher>();
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

export function registerReloadFlusher(id: string, flusher: ReloadFlusher): () => void {
  flushers.set(id, flusher);
  return () => {
    if (flushers.get(id) === flusher) flushers.delete(id);
  };
}

export function flushReloadPersistence(): boolean {
  let flushed = true;
  for (const flusher of flushers.values()) {
    try {
      flusher();
    } catch {
      flushed = false;
    }
  }
  return flushed;
}

export function resetReloadCoordinationForTests(): void {
  blockers.clear();
  flushers.clear();
  publish();
}
