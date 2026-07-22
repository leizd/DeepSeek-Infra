import { useCallback, useMemo } from "react";
import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addMemory,
  clearMemories,
  deleteMemory,
  listMemories,
  MemoryConflictError,
  type MemoryEntry,
} from "../../api/memoryApi";
import { MEMORIES_QUERY_KEY } from "../../app/queryKeys";
import { mutationKeys } from "../../app/mutationKeys";
import { latestCacheMutationError, type MutationStateSnapshot } from "../../app/mutationErrors";
import { useActionLocks } from "../../shared/useActionLocks";

export { MEMORIES_QUERY_KEY };

export interface MemorySaveResult {
  saved: boolean;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export interface MemoryController {
  memories: readonly MemoryEntry[];
  loading: boolean;
  refreshing: boolean;
  clearing: boolean;
  error: string;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  remove(memoryId: string): Promise<void>;
  clear(): Promise<void>;
  isRemovingMemory(memoryId: string): boolean;
  save(input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] }): Promise<MemorySaveResult>;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function useMemoryController(): MemoryController {
  const queryClient = useQueryClient();
  const runLocked = useActionLocks();

  const memoriesQuery = useQuery<MemoryEntry[]>({
    queryKey: MEMORIES_QUERY_KEY,
    queryFn: ({ signal }) => listMemories({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY }),
    [queryClient],
  );

  const clearMutation = useMutation({
    mutationKey: mutationKeys.memories.clear,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
    },
    mutationFn: () => clearMemories(),
    onSuccess: () => {
      queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, []);
    },
    onSettled: () => void invalidate(),
  });

  const removingMemoryIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.memories.remove, status: "pending" },
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
    const cache = queryClient.getMutationCache();
    for (const key of [mutationKeys.memories.remove, mutationKeys.memories.clear]) {
      cache.findAll({ mutationKey: key }).forEach((m) => cache.remove(m));
    }
    await invalidate();
  }, [invalidate, queryClient]);

  const remove = useCallback(
    async (memoryId: string) => {
      await runLocked(`memory:remove:${memoryId}`, async () => {
        await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.memories.remove,
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
    },
    [invalidate, queryClient, runLocked],
  );

  const clear = useCallback(async () => {
    await runLocked("memory:clear", () => clearMutation.mutateAsync());
  }, [clearMutation, runLocked]);

  const save = useCallback(
    async (input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] }): Promise<MemorySaveResult> => {
      try {
        await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
        const saved = await addMemory(input);
        queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, (current) => {
          if (!current) return current;
          const replaced = new Set(input.replaceIds ?? []);
          return [...current.filter((entry) => entry.id !== saved.id && !replaced.has(entry.id)), saved];
        });
        void invalidate();
        return { saved: true, conflicts: [] };
      } catch (reason) {
        if (reason instanceof MemoryConflictError) {
          return { saved: false, conflicts: reason.conflicts };
        }
        throw reason;
      }
    },
    [invalidate, queryClient],
  );

  const mutationErrors = useMutationState<MutationStateSnapshot>({
    filters: { predicate: (mutation) => { const key = mutation.options.mutationKey; return Array.isArray(key) && key.length >= 2 && key[0] === "memories"; } },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
    }),
  });

  const firstError = memoriesQuery.error ?? latestCacheMutationError(mutationErrors);

  return {
    memories: memoriesQuery.data ?? [],
    loading: memoriesQuery.isLoading,
    refreshing: memoriesQuery.isFetching && !memoriesQuery.isLoading,
    clearing: clearMutation.isPending,
    error: firstError ? errorText(firstError, "记忆操作失败") : "",
    refresh,
    recover,
    remove,
    clear,
    isRemovingMemory,
    save,
  };
}
