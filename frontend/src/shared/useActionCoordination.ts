import { useCallback, useState } from "react";

import {
  actionLockValue,
  EntityActionConflictError,
  type EntityActionLockResult,
} from "./useEntityActionLocks";

export function useActionCoordination() {
  const [coordinationError, setCoordinationError] = useState("");

  const resolveAction = useCallback(<T,>(
    result: EntityActionLockResult<T>,
    entityKey: string,
    operation: string,
  ): T => {
    try {
      const value = actionLockValue(result, entityKey, operation);
      setCoordinationError("");
      return value;
    } catch (reason) {
      if (reason instanceof EntityActionConflictError) setCoordinationError(reason.message);
      throw reason;
    }
  }, []);

  const clearCoordinationError = useCallback(() => setCoordinationError(""), []);

  return { coordinationError, resolveAction, clearCoordinationError };
}
