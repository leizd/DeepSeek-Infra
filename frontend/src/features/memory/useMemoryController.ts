import { useCallback, useMemo } from "react";
import { useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addMemory,
  clearMemories,
  deleteMemory,
  listMemories,
  MemoryConflictError,
  type MemoryEntry,
} from "../../api/memoryApi";
import { MEMORIES_QUERY_KEY } from "../../app/queryKeys";
import { MEMORY_LIST_MUTATION_KEYS, mutationKeys, ownsMutationKey } from "../../app/mutationKeys";
import { stableIntentKey } from "../../app/mutationIntents";
import {
  isLifecycleMutationMeta,
  isMutationActive,
  removeFailedMutations,
  type LifecycleMutationMeta,
  useMutationActivity,
} from "../../app/mutationLifecycle";
import {
  latestUnresolvedLifecycleError,
  type LifecycleMutationSnapshot,
} from "../../app/mutationErrors";
import { useActionCoordination } from "../../shared/useActionCoordination";
import { useMemoryWriteBarrier } from "./useMemoryWriteBarrier";

export { MEMORIES_QUERY_KEY };

export interface MemorySaveResult {
  saved: boolean;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export interface MemorySaveInput {
  content: string;
  category?: string;
  scope?: string;
  replaceIds?: readonly string[];
}

type MemorySaveOutcome =
  | { saved: true; entry: MemoryEntry; conflicts: readonly [] }
  | { saved: false; conflicts: readonly { id: string; content: string; reason: string }[] };

export interface MemoryController {
  memories: readonly MemoryEntry[];
  loading: boolean;
  refreshing: boolean;
  clearing: boolean;
  saving: boolean;
  hasPendingWrites: boolean;
  error: string;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  remove(memoryId: string): Promise<void>;
  clear(): Promise<void>;
  isRemovingMemory(memoryId: string): boolean;
  save(input: MemorySaveInput): Promise<MemorySaveResult>;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function memorySaveIntent(input: MemorySaveInput): string {
  return stableIntentKey({
    content: input.content.trim(),
    category: input.category ?? "fact",
    scope: input.scope ?? "global",
    replaceIds: [...(input.replaceIds ?? [])].sort(),
  });
}

function memoryMutationMeta(
  lifecycleId: string,
  entityKey: string,
  operation: string,
  intentKey: string,
): LifecycleMutationMeta {
  return { owner: "memory-list", lifecycleId, entityKey, operation, intentKey };
}

export function useMemoryController(): MemoryController {
  const queryClient = useQueryClient();
  const { runWrite, runClear } = useMemoryWriteBarrier();
  const { coordinationError, resolveAction, clearCoordinationError } = useActionCoordination();

  const memoriesQuery = useQuery<MemoryEntry[]>({
    queryKey: MEMORIES_QUERY_KEY,
    queryFn: ({ signal }) => listMemories({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY }),
    [queryClient],
  );

  const clearActivity = useMutationActivity(mutationKeys.memoryList.clear);
  const saveActivity = useMutationActivity(mutationKeys.memoryList.save);

  const removingMemoryIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.memoryList.remove,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as string | undefined) ?? "",
  });
  const removingMemoryIdSet = useMemo(() => new Set(removingMemoryIds), [removingMemoryIds]);
  const isRemovingMemory = useCallback(
    (memoryId: string) => removingMemoryIdSet.has(memoryId),
    [removingMemoryIdSet],
  );
  const pendingWriteMutations = useMutationState<number>({
    filters: {
      predicate: (mutation) => isMutationActive(mutation.state) && ownsMutationKey(
        mutation.options.mutationKey,
        [mutationKeys.memoryList.save, mutationKeys.memoryList.remove],
      ),
    },
    select: () => 1,
  });

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const recover = useCallback(async () => {
    clearCoordinationError();
    removeFailedMutations(queryClient, MEMORY_LIST_MUTATION_KEYS);
    await queryClient.refetchQueries({ queryKey: MEMORIES_QUERY_KEY, type: "active" });
  }, [clearCoordinationError, queryClient]);

  const remove = useCallback(
    async (memoryId: string) => {
      const entityKey = `memory:${memoryId}`;
      const operation = "remove";
      const intentKey = memoryId;
      const result = await runWrite(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.memoryList.remove,
          meta: memoryMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
          },
          mutationFn: (id: string) => deleteMemory(id),
          onSuccess: (_result, id) => {
            queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, (current) =>
              (current ?? []).filter((entry) => entry.id !== id),
            );
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute(memoryId);
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runWrite],
  );

  const clear = useCallback(async () => {
    const entityKey = "memory-list:clear";
    const operation = "clear";
    const intentKey = "clear";
    const result = await runClear(async (lifecycleId) => {
      const mutation = queryClient.getMutationCache().build(queryClient, {
        mutationKey: mutationKeys.memoryList.clear,
        meta: memoryMutationMeta(lifecycleId, entityKey, operation, intentKey),
        onMutate: async () => {
          await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
        },
        mutationFn: () => clearMemories(),
        onSuccess: () => {
          queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, []);
        },
        onSettled: () => void invalidate(),
      });
      return mutation.execute(undefined);
    });
    resolveAction(result, entityKey, operation);
  }, [invalidate, queryClient, resolveAction, runClear]);

  const save = useCallback(
    async (input: MemorySaveInput): Promise<MemorySaveResult> => {
      const intentKey = memorySaveIntent(input);
      const entityKey = `memory-save:${intentKey}`;
      const operation = "save";
      const result = await runWrite(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.memoryList.save,
          meta: memoryMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
          },
          mutationFn: async (value: MemorySaveInput): Promise<MemorySaveOutcome> => {
            try {
              return { saved: true, entry: await addMemory(value), conflicts: [] };
            } catch (reason) {
              if (reason instanceof MemoryConflictError) return { saved: false, conflicts: reason.conflicts };
              throw reason;
            }
          },
          onSuccess: (outcome, value) => {
            if (!outcome.saved) return;
            queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, (current) => {
              if (!current) return current;
              const replaced = new Set(value.replaceIds ?? []);
              return [...current.filter((entry) => entry.id !== outcome.entry.id && !replaced.has(entry.id)), outcome.entry];
            });
          },
          onSettled: (outcome) => {
            if (outcome?.saved) void invalidate();
          },
        });
        return mutation.execute(input);
      });
      const outcome = resolveAction(result, entityKey, operation);
      return { saved: outcome.saved, conflicts: outcome.conflicts };
    },
    [invalidate, queryClient, resolveAction, runWrite],
  );

  const mutationErrors = useMutationState<LifecycleMutationSnapshot>({
    filters: { predicate: (mutation) => ownsMutationKey(mutation.options.mutationKey, MEMORY_LIST_MUTATION_KEYS) },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
      meta: isLifecycleMutationMeta(mutation.options.meta) ? mutation.options.meta : undefined,
    }),
  });

  const firstError = memoriesQuery.error ?? latestUnresolvedLifecycleError(mutationErrors);

  return {
    memories: memoriesQuery.data ?? [],
    loading: memoriesQuery.isLoading,
    refreshing: memoriesQuery.isFetching && !memoriesQuery.isLoading,
    clearing: clearActivity.active,
    saving: saveActivity.active,
    hasPendingWrites: pendingWriteMutations.length > 0,
    error: coordinationError || (firstError ? errorText(firstError, "记忆操作失败") : ""),
    refresh,
    recover,
    remove,
    clear,
    isRemovingMemory,
    save,
  };
}
