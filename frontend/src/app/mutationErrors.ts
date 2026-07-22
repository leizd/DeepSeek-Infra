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

export function latestCacheMutationError(mutations: readonly MutationStateSnapshot[]): unknown {
  let latest: MutationStateSnapshot | null = null;
  for (const m of mutations) {
    if (!latest || m.submittedAt > latest.submittedAt) latest = m;
  }
  return latest?.status === "error" ? latest.error : undefined;
}
