import { useCallback, useState } from "react";

import {
  addMemory,
  clearMemories,
  deleteMemory,
  listMemories,
  MemoryConflictError,
  type MemoryEntry,
} from "../../api/memoryApi";

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

export function useMemoryController(): MemoryController {
  const [memories, setMemories] = useState<readonly MemoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setMemories(await listMemories());
    } catch (reason) {
      setError(reason instanceof Error && reason.message ? reason.message : "记忆加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const remove = useCallback(async (memoryId: string) => {
    await deleteMemory(memoryId);
    setMemories((current) => current.filter((memory) => memory.id !== memoryId));
  }, []);

  const clear = useCallback(async () => {
    await clearMemories();
    setMemories([]);
  }, []);

  const save = useCallback(
    async (input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] }): Promise<MemorySaveResult> => {
      try {
        await addMemory(input);
        void refresh();
        return { saved: true, conflicts: [] };
      } catch (reason) {
        if (reason instanceof MemoryConflictError) {
          return { saved: false, conflicts: reason.conflicts };
        }
        throw reason;
      }
    },
    [refresh],
  );

  return { memories, loading, error, refresh, remove, clear, save };
}
