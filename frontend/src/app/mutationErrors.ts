import type { LifecycleMutationMeta } from "./mutationLifecycle";

export interface MutationErrorState {
  status: string;
  error: unknown;
  submittedAt: number;
}

export function latestMutationError(...mutations: MutationErrorState[]): unknown {
  let latest: MutationErrorState | null = null;
  for (const mutation of mutations) {
    if (!latest || mutation.submittedAt > latest.submittedAt) latest = mutation;
  }
  return latest?.status === "error" ? latest.error : null;
}

export interface MutationStateSnapshot {
  status: string;
  error: unknown;
  submittedAt: number;
}

export interface LifecycleMutationSnapshot extends MutationStateSnapshot {
  meta?: LifecycleMutationMeta;
}

export function latestCacheMutationError(mutations: readonly MutationStateSnapshot[]): unknown {
  let latest: MutationStateSnapshot | null = null;
  for (const m of mutations) {
    if (!latest || m.submittedAt > latest.submittedAt) latest = m;
  }
  return latest?.status === "error" ? latest.error : undefined;
}

export function latestUnresolvedLifecycleError(
  mutations: readonly LifecycleMutationSnapshot[],
): unknown {
  const latestByEntity = new Map<string, LifecycleMutationSnapshot>();
  for (const mutation of mutations) {
    const entityKey = mutation.meta?.entityKey ?? "__unowned__";
    const current = latestByEntity.get(entityKey);
    if (!current || mutation.submittedAt >= current.submittedAt) {
      latestByEntity.set(entityKey, mutation);
    }
  }

  let latestFailure: LifecycleMutationSnapshot | undefined;
  for (const mutation of latestByEntity.values()) {
    if (mutation.status !== "error") continue;
    if (!latestFailure || mutation.submittedAt >= latestFailure.submittedAt) {
      latestFailure = mutation;
    }
  }
  return latestFailure?.error;
}
