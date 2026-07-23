import { useCallback, useMemo } from "react";
import { useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import { clearMemories, deleteMemory, listMemories, type MemoryEntry } from "../../api/memoryApi";
import { MEMORIES_QUERY_KEY } from "../../app/queryKeys";
import { MEMORY_LIST_MUTATION_KEYS, mutationKeys, ownsMutationKey } from "../../app/mutationKeys";
import {
  isLifecycleMutationMeta,
  isMutationActive,
  removeFailedMutations,
  type LifecycleMutationMeta,
} from "../../app/mutationLifecycle";
import { latestUnresolvedLifecycleError, type LifecycleMutationSnapshot } from "../../app/mutationErrors";
import { useActionCoordination } from "../../shared/useActionCoordination";
import { useMemoryWriteBarrier } from "./useMemoryWriteBarrier";
import {
  memorySaveIntent,
  useMemoryWriteController,
  type MemorySaveInput,
  type MemorySaveResult,
  type MemoryWriteController,
} from "./useMemoryWriteController";

export { MEMORIES_QUERY_KEY, memorySaveIntent };
export type { MemorySaveInput, MemorySaveResult };

export interface MemoryController extends MemoryWriteController {
  memories: readonly MemoryEntry[];
  loading: boolean;
  refreshing: boolean;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  remove(memoryId: string): Promise<void>;
  clear(): Promise<void>;
  isRemovingMemory(memoryId: string): boolean;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function memoryMutationMeta(
  lifecycleId: string,
  entityKey: string,
  operation: string,
  intentKey: string,
): LifecycleMutationMeta {
  return { owner: "memory-list", lifecycleId, entityKey, operation, intentKey };
}

export function useMemoryListController(write: MemoryWriteController): MemoryController {
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
  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);
  const recover = useCallback(async () => {
    clearCoordinationError();
    write.recoverWrites();
    removeFailedMutations(queryClient, MEMORY_LIST_MUTATION_KEYS);
    await queryClient.refetchQueries({ queryKey: MEMORIES_QUERY_KEY, type: "active" });
  }, [clearCoordinationError, queryClient, write]);
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
        onSuccess: () => queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, []),
        onSettled: () => void invalidate(),
      });
      return mutation.execute(undefined);
    });
    resolveAction(result, entityKey, operation);
  }, [invalidate, queryClient, resolveAction, runClear]);
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
    ...write,
    memories: memoriesQuery.data ?? [],
    loading: memoriesQuery.isLoading,
    refreshing: memoriesQuery.isFetching && !memoriesQuery.isLoading,
    error: coordinationError || write.error || (firstError ? errorText(firstError, "记忆操作失败") : ""),
    refresh,
    recover,
    remove,
    clear,
    isRemovingMemory,
  };
}

export function useMemoryController(): MemoryController {
  return useMemoryListController(useMemoryWriteController());
}
