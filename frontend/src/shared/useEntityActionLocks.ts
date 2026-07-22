import { useCallback, useRef } from "react";

export type EntityActionLockResult<T> =
  | { status: "executed"; value: T }
  | { status: "deduplicated"; value: T }
  | { status: "conflict" };

export class EntityActionConflictError extends Error {
  readonly entityKey: string;
  readonly operation: string;

  constructor(entityKey: string, operation: string) {
    super("另一项相关操作仍在执行，请稍后重试");
    this.name = "EntityActionConflictError";
    this.entityKey = entityKey;
    this.operation = operation;
  }
}

export function actionLockValue<T>(
  result: EntityActionLockResult<T>,
  entityKey: string,
  operation: string,
): T {
  if (result.status === "conflict") throw new EntityActionConflictError(entityKey, operation);
  return result.value;
}

export function useEntityActionLocks() {
  const locks = useRef(new Map<string, { operation: string; promise: Promise<unknown> }>());

  return useCallback(
    async <T,>(entityKey: string, operation: string, action: () => Promise<T>): Promise<EntityActionLockResult<T>> => {
      const existing = locks.current.get(entityKey);
      if (existing) {
        if (existing.operation !== operation) return { status: "conflict" };
        return { status: "deduplicated", value: await existing.promise as T };
      }

      const promise = action();
      locks.current.set(entityKey, { operation, promise });
      try {
        return { status: "executed", value: await promise };
      } finally {
        if (locks.current.get(entityKey)?.promise === promise) locks.current.delete(entityKey);
      }
    },
    [],
  );
}
