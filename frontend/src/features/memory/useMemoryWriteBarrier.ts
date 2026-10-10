import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { activeLifecycleMutation } from "../../app/mutationLifecycle";
import type { ActionBlocker, EntityActionLockResult } from "../../shared/useEntityActionLocks";
import { lifecycleMutationBlocker, useEntityActionLocks } from "../../shared/useEntityActionLocks";

interface MemoryBarrierState {
  writes: Map<string, ActionBlocker>;
  clear: ActionBlocker | null;
}

const memoryBarrierStates = new WeakMap<QueryClient, MemoryBarrierState>();

function memoryBarrierState(queryClient: QueryClient): MemoryBarrierState {
  const existing = memoryBarrierStates.get(queryClient);
  if (existing) return existing;
  const created = { writes: new Map<string, ActionBlocker>(), clear: null };
  memoryBarrierStates.set(queryClient, created);
  return created;
}

export function useMemoryWriteBarrier() {
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();

  const runWrite = useCallback(
    async <T,>(
      entityKey: string,
      operation: string,
      intentKey: string,
      action: (lifecycleId: string) => Promise<T>,
    ): Promise<EntityActionLockResult<T>> => {
      const barrier = memoryBarrierState(queryClient);
      const cachedClear = activeLifecycleMutation(
        queryClient,
        (meta) => meta.owner === "memory-list" && meta.operation === "clear",
      );
      if (barrier.clear || cachedClear) {
        return {
          status: "conflict",
          blocker: barrier.clear ?? lifecycleMutationBlocker(cachedClear!),
        };
      }

      return runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
        const blocker: ActionBlocker = {
          lifecycleId,
          entityKey,
          operation,
          intentKey,
          source: "local-lock",
        };
        barrier.writes.set(entityKey, blocker);
        try {
          return await action(lifecycleId);
        } finally {
          if (barrier.writes.get(entityKey)?.lifecycleId === lifecycleId) {
            barrier.writes.delete(entityKey);
          }
        }
      });
    },
    [queryClient, runEntityAction],
  );

  const runClear = useCallback(async <T,>(action: (lifecycleId: string) => Promise<T>): Promise<EntityActionLockResult<T>> => {
    const barrier = memoryBarrierState(queryClient);
    const localWrite = barrier.writes.values().next().value as ActionBlocker | undefined;
    const cachedWrite = activeLifecycleMutation(
      queryClient,
      (meta) => meta.owner === "memory-list" && meta.operation !== "clear",
    );
    if (localWrite || cachedWrite) {
      return {
        status: "conflict",
        blocker: localWrite ?? lifecycleMutationBlocker(cachedWrite!),
      };
    }

    return runEntityAction("memory-list:clear", "clear", "clear", async (lifecycleId) => {
      const blocker: ActionBlocker = {
        lifecycleId,
        entityKey: "memory-list:clear",
        operation: "clear",
        intentKey: "clear",
        source: "local-lock",
      };
      barrier.clear = blocker;
      try {
        return await action(lifecycleId);
      } finally {
        if (barrier.clear?.lifecycleId === lifecycleId) barrier.clear = null;
      }
    });
  }, [queryClient, runEntityAction]);

  return { runWrite, runClear };
}
