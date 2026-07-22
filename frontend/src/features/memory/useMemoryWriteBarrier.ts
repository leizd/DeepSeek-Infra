import { useCallback, useRef } from "react";

import type { EntityActionLockResult } from "../../shared/useEntityActionLocks";

export function useMemoryWriteBarrier() {
  const writes = useRef(new Map<string, Promise<unknown>>());
  const clear = useRef<Promise<unknown> | null>(null);

  const runWrite = useCallback(
    async <T,>(operationKey: string, action: () => Promise<T>): Promise<EntityActionLockResult<T>> => {
      if (clear.current) return { status: "conflict" };
      const existing = writes.current.get(operationKey);
      if (existing) return { status: "deduplicated", value: await existing as T };

      const promise = action();
      writes.current.set(operationKey, promise);
      try {
        return { status: "executed", value: await promise };
      } finally {
        if (writes.current.get(operationKey) === promise) writes.current.delete(operationKey);
      }
    },
    [],
  );

  const runClear = useCallback(async <T,>(action: () => Promise<T>): Promise<EntityActionLockResult<T>> => {
    if (clear.current) return { status: "deduplicated", value: await clear.current as T };
    if (writes.current.size) return { status: "conflict" };

    const promise = action();
    clear.current = promise;
    try {
      return { status: "executed", value: await promise };
    } finally {
      if (clear.current === promise) clear.current = null;
    }
  }, []);

  return { runWrite, runClear };
}
