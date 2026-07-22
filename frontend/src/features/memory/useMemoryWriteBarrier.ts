import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { activeLifecycleMutation } from "../../app/mutationLifecycle";
import type { EntityActionLockResult } from "../../shared/useEntityActionLocks";
import { useEntityActionLocks } from "../../shared/useEntityActionLocks";

export function useMemoryWriteBarrier() {
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();
  const writes = useRef(new Map<string, string>());
  const clear = useRef(false);

  const runWrite = useCallback(
    async <T,>(
      entityKey: string,
      operation: string,
      intentKey: string,
      action: () => Promise<T>,
    ): Promise<EntityActionLockResult<T>> => {
      const cachedClear = activeLifecycleMutation(
        queryClient,
        (meta) => meta.owner === "memory-list" && meta.operation === "clear",
      );
      if (clear.current || cachedClear) return { status: "conflict", activeOperation: "clear" };

      return runEntityAction(entityKey, operation, intentKey, async () => {
        writes.current.set(entityKey, operation);
        try {
          return await action();
        } finally {
          writes.current.delete(entityKey);
        }
      });
    },
    [queryClient, runEntityAction],
  );

  const runClear = useCallback(async <T,>(action: () => Promise<T>): Promise<EntityActionLockResult<T>> => {
    const localWrite = writes.current.values().next().value as string | undefined;
    const cachedWrite = activeLifecycleMutation(
      queryClient,
      (meta) => meta.owner === "memory-list" && meta.operation !== "clear",
    );
    if (localWrite || cachedWrite) {
      return { status: "conflict", activeOperation: localWrite ?? cachedWrite?.operation ?? "save" };
    }

    return runEntityAction("memory-list:clear", "clear", "clear", async () => {
      clear.current = true;
      try {
        return await action();
      } finally {
        clear.current = false;
      }
    });
  }, [queryClient, runEntityAction]);

  return { runWrite, runClear };
}
