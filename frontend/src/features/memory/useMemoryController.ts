import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addMemory,
  clearMemories,
  deleteMemory,
  listMemories,
  MemoryConflictError,
  type MemoryEntry,
} from "../../api/memoryApi";
import { MEMORIES_QUERY_KEY } from "../../app/queryKeys";
import { latestMutationError } from "../../app/mutationErrors";

export { MEMORIES_QUERY_KEY };

export interface MemorySaveResult {
  saved: boolean;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export interface MemoryController {
  memories: readonly MemoryEntry[];
  loading: boolean;
  refreshing: boolean;
  removingMemoryId: string | null;
  clearing: boolean;
  error: string;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  remove(memoryId: string): Promise<void>;
  clear(): Promise<void>;
  save(input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] }): Promise<MemorySaveResult>;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function useMemoryController(): MemoryController {
  const queryClient = useQueryClient();

  const memoriesQuery = useQuery<MemoryEntry[]>({
    queryKey: MEMORIES_QUERY_KEY,
    queryFn: ({ signal }) => listMemories({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY }),
    [queryClient],
  );

  const removeMutation = useMutation({
    mutationFn: (memoryId: string) => deleteMemory(memoryId),
    onSuccess: (_result, memoryId) => {
      queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, (current) =>
        (current ?? []).filter((entry) => entry.id !== memoryId),
      );
      void invalidate();
    },
  });

  const clearMutation = useMutation({
    mutationFn: () => clearMemories(),
    onSuccess: () => {
      queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, []);
      void invalidate();
    },
  });

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const recover = useCallback(async () => {
    if (removeMutation.isError) removeMutation.reset();
    if (clearMutation.isError) clearMutation.reset();
    await invalidate();
  }, [clearMutation, invalidate, removeMutation]);

  const remove = useCallback(
    async (memoryId: string) => {
      await removeMutation.mutateAsync(memoryId);
    },
    [removeMutation],
  );

  const clear = useCallback(async () => {
    await clearMutation.mutateAsync();
  }, [clearMutation]);

  const save = useCallback(
    async (input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] }): Promise<MemorySaveResult> => {
      try {
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

  const firstError = memoriesQuery.error ?? latestMutationError(removeMutation, clearMutation);

  return {
    memories: memoriesQuery.data ?? [],
    loading: memoriesQuery.isLoading,
    refreshing: memoriesQuery.isFetching && !memoriesQuery.isLoading,
    removingMemoryId: removeMutation.isPending ? (removeMutation.variables ?? null) : null,
    clearing: clearMutation.isPending,
    error: firstError ? errorText(firstError, "记忆操作失败") : "",
    refresh,
    recover,
    remove,
    clear,
    save,
  };
}
