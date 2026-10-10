import { useMutationState, type MutationKey, type QueryClient } from "@tanstack/react-query";

export type LifecycleMutationOwner =
  | "project-list"
  | "project-binding"
  | "skill-list"
  | "memory-list";

export interface LifecycleMutationMeta extends Record<string, unknown> {
  owner: LifecycleMutationOwner;
  lifecycleId: string;
  entityKey: string;
  operation: string;
  intentKey: string;
}

export function isLifecycleMutationMeta(value: unknown): value is LifecycleMutationMeta {
  if (!value || typeof value !== "object") return false;
  const meta = value as Partial<LifecycleMutationMeta>;
  return typeof meta.owner === "string"
    && typeof meta.lifecycleId === "string"
    && typeof meta.entityKey === "string"
    && typeof meta.operation === "string"
    && typeof meta.intentKey === "string";
}

export function createLifecycleId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `lifecycle-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function isMutationActive(state: { status: string; isPaused: boolean }): boolean {
  return state.status === "pending" || state.isPaused;
}

export function useMutationActivity(mutationKey: MutationKey): { active: boolean; count: number } {
  const activeMutations = useMutationState<number>({
    filters: {
      mutationKey,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: () => 1,
  });
  return { active: activeMutations.length > 0, count: activeMutations.length };
}

export function activeLifecycleMutation(
  queryClient: QueryClient,
  predicate: (meta: LifecycleMutationMeta) => boolean,
): LifecycleMutationMeta | undefined {
  const matches = queryClient.getMutationCache().findAll({
    predicate: (mutation) => {
      const meta = mutation.options.meta;
      return isMutationActive(mutation.state) && isLifecycleMutationMeta(meta) && predicate(meta);
    },
  });
  const latest = matches.reduce<(typeof matches)[number] | undefined>(
    (current, mutation) => !current || mutation.state.submittedAt > current.state.submittedAt ? mutation : current,
    undefined,
  );
  const meta = latest?.options.meta;
  return isLifecycleMutationMeta(meta) ? meta : undefined;
}

export function removeFailedMutations(
  queryClient: QueryClient,
  keys: readonly (readonly unknown[])[],
): void {
  const cache = queryClient.getMutationCache();
  for (const key of keys) {
    for (const mutation of cache.findAll({ mutationKey: key, exact: true })) {
      if (mutation.state.status === "error") cache.remove(mutation);
    }
  }
}
