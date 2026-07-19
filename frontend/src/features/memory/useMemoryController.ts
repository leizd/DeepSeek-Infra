import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addMemory,
  clearMemories,
  deleteMemory,
  listMemories,
  MemoryConflictError,
  type MemoryEntry,
} from "../../api/memoryApi";

export const MEMORIES_QUERY_KEY = ["memories"] as const;

export interface MemorySaveResult {
  saved: boolean;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export interface MemoryController {
  memories: readonly MemoryEntry[];
  loading: boolean;
  error: string;
  refresh(): Promise<void>;
  remove(memoryId: string): Promise<void>;
  clear(): Promise<void>;
  save(input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] }): Promise<MemorySaveResult>;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function useMemoryController(): MemoryController {
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState("");

  const memoriesQuery = useQuery<MemoryEntry[]>({
    queryKey: MEMORIES_QUERY_KEY,
    queryFn: () => listMemories(),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY }),
    [queryClient],
  );

  const removeMutation = useMutation({
    mutationFn: (memoryId: string) => deleteMemory(memoryId),
    onSuccess: () => {
      setActionError("");
      void invalidate();
    },
    onError: (reason) => setActionError(errorText(reason, "记忆删除失败")),
  });

  const clearMutation = useMutation({
    mutationFn: () => clearMemories(),
    onSuccess: () => {
      setActionError("");
      void invalidate();
    },
    onError: (reason) => setActionError(errorText(reason, "记忆清空失败")),
  });

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

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
        await addMemory(input);
        void invalidate();
        return { saved: true, conflicts: [] };
      } catch (reason) {
        if (reason instanceof MemoryConflictError) {
          return { saved: false, conflicts: reason.conflicts };
        }
        throw reason;
      }
    },
    [invalidate],
  );

  return {
    memories: memoriesQuery.data ?? [],
    loading: memoriesQuery.isLoading,
    error: actionError || (memoriesQuery.error ? errorText(memoriesQuery.error, "记忆加载失败") : ""),
    refresh,
    remove,
    clear,
    save,
  };
}
