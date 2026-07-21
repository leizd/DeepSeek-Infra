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
