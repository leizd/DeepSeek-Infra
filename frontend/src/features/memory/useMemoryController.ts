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
import { MEMORY_LIST_MUTATION_KEYS, mutationKeys, ownsMutationKey } from "../../app/mutationKeys";
import { removeFailedMutations } from "../../app/mutationLifecycle";
import { latestCacheMutationError, type MutationStateSnapshot } from "../../app/mutationErrors";
import { actionLockValue } from "../../shared/useEntityActionLocks";
import { useMemoryWriteBarrier } from "./useMemoryWriteBarrier";

export { MEMORIES_QUERY_KEY };

export interface MemorySaveResult {
  saved: boolean;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

interface MemorySaveInput {
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

export function useMemoryController(): MemoryController {
  const queryClient = useQueryClient();
  const { runWrite, runClear } = useMemoryWriteBarrier();

  const memoriesQuery = useQuery<MemoryEntry[]>({
    queryKey: MEMORIES_QUERY_KEY,
    queryFn: ({ signal }) => listMemories({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY }),
    [queryClient],
  );

  const clearMutation = useMutation({
    mutationKey: mutationKeys.memoryList.clear,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
    },
    mutationFn: () => clearMemories(),
    onSuccess: () => {
      queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, []);
    },
    onSettled: () => void invalidate(),
  });

  const saveMutation = useMutation<MemorySaveOutcome, unknown, MemorySaveInput>({
    mutationKey: mutationKeys.memoryList.save,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
    },
    mutationFn: async (input) => {
      try {
        return { saved: true, entry: await addMemory(input), conflicts: [] };
      } catch (reason) {
        if (reason instanceof MemoryConflictError) return { saved: false, conflicts: reason.conflicts };
        throw reason;
      }
    },
    onSuccess: (outcome, input) => {
      if (!outcome.saved) return;
      queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, (current) => {
        if (!current) return current;
        const replaced = new Set(input.replaceIds ?? []);
        return [...current.filter((entry) => entry.id !== outcome.entry.id && !replaced.has(entry.id)), outcome.entry];
      });
    },
    onSettled: (outcome) => {
      if (outcome?.saved) void invalidate();
    },
  });

  const removingMemoryIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.memoryList.remove, exact: true, status: "pending" },
    select: (mutation) => (mutation.state.variables as string | undefined) ?? "",
  });
  const removingMemoryIdSet = useMemo(() => new Set(removingMemoryIds), [removingMemoryIds]);
  const isRemovingMemory = useCallback(
    (memoryId: string) => removingMemoryIdSet.has(memoryId),
    [removingMemoryIdSet],
  );
  const savingMutations = useMutationState<number>({
    filters: { mutationKey: mutationKeys.memoryList.save, exact: true, status: "pending" },
    select: () => 1,
  });
  const pendingWriteMutations = useMutationState<number>({
    filters: {
      status: "pending",
      predicate: (mutation) => ownsMutationKey(
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
    removeFailedMutations(queryClient, MEMORY_LIST_MUTATION_KEYS);
    await queryClient.refetchQueries({ queryKey: MEMORIES_QUERY_KEY, type: "active" });
  }, [queryClient]);

  const remove = useCallback(
    async (memoryId: string) => {
      const operationKey = `remove:${memoryId}`;
      const result = await runWrite(operationKey, async () => {
        await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.memoryList.remove,
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
      actionLockValue(result, "memory", operationKey);
    },
    [invalidate, queryClient, runWrite],
  );

  const clear = useCallback(async () => {
    const result = await runClear(() => clearMutation.mutateAsync());
    actionLockValue(result, "memory", "clear");
  }, [clearMutation, runClear]);

  const save = useCallback(
    async (input: MemorySaveInput): Promise<MemorySaveResult> => {
      const operationKey = `save:${JSON.stringify(input)}`;
      const result = await runWrite(operationKey, () => saveMutation.mutateAsync(input));
      const outcome = actionLockValue(result, "memory", "save");
      return { saved: outcome.saved, conflicts: outcome.conflicts };
    },
    [runWrite, saveMutation],
  );

  const mutationErrors = useMutationState<MutationStateSnapshot>({
    filters: { predicate: (mutation) => ownsMutationKey(mutation.options.mutationKey, MEMORY_LIST_MUTATION_KEYS) },
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
    saving: savingMutations.length > 0,
    hasPendingWrites: pendingWriteMutations.length > 0,
    error: firstError ? errorText(firstError, "记忆操作失败") : "",
    refresh,
    recover,
    remove,
    clear,
    isRemovingMemory,
    save,
  };
}
