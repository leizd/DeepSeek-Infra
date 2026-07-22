import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { activeLifecycleMutation } from "../../app/mutationLifecycle";
import type { ActionBlocker, EntityActionLockResult } from "../../shared/useEntityActionLocks";
import { lifecycleMutationBlocker, useEntityActionLocks } from "../../shared/useEntityActionLocks";

export function useMemoryWriteBarrier() {
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();
  const writes = useRef(new Map<string, ActionBlocker>());
  const clear = useRef<ActionBlocker | null>(null);

  const runWrite = useCallback(
    async <T,>(
      entityKey: string,
      operation: string,
      intentKey: string,
      action: (lifecycleId: string) => Promise<T>,
    ): Promise<EntityActionLockResult<T>> => {
      const cachedClear = activeLifecycleMutation(
        queryClient,
        (meta) => meta.owner === "memory-list" && meta.operation === "clear",
      );
      if (clear.current || cachedClear) {
        return {
          status: "conflict",
          blocker: clear.current ?? lifecycleMutationBlocker(cachedClear!),
        };
      }

      return runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
        writes.current.set(entityKey, {
          lifecycleId,
          entityKey,
          operation,
          intentKey,
          source: "local-lock",
        });
        try {
          return await action(lifecycleId);
        } finally {
          writes.current.delete(entityKey);
        }
      });
    },
    [queryClient, runEntityAction],
  );

  const runClear = useCallback(async <T,>(action: (lifecycleId: string) => Promise<T>): Promise<EntityActionLockResult<T>> => {
    const localWrite = writes.current.values().next().value as ActionBlocker | undefined;
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
      clear.current = {
        lifecycleId,
        entityKey: "memory-list:clear",
        operation: "clear",
        intentKey: "clear",
        source: "local-lock",
      };
      try {
        return await action(lifecycleId);
      } finally {
        clear.current = null;
      }
    });
  }, [queryClient, runEntityAction]);

  return { runWrite, runClear };
}
