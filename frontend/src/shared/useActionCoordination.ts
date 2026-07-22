import { useMutationState } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import {
  isLifecycleMutationMeta,
  isMutationActive,
} from "../app/mutationLifecycle";

import {
  actionLockValue,
  EntityActionConflictError,
  type EntityActionLockResult,
} from "./useEntityActionLocks";

export interface CoordinationFailure {
  entityKey: string;
  requestedOperation: string;
  activeOperation: string;
  message: string;
}

export function useActionCoordination() {
  const [coordinationFailure, setCoordinationFailure] = useState<CoordinationFailure | null>(null);
  const activeEntityKeys = useMutationState<string>({
    filters: {
      predicate: (mutation) =>
        isMutationActive(mutation.state) && isLifecycleMutationMeta(mutation.options.meta),
    },
    select: (mutation) => isLifecycleMutationMeta(mutation.options.meta)
      ? mutation.options.meta.entityKey
      : "",
  });

  useEffect(() => {
    if (!coordinationFailure) return;
    if (!activeEntityKeys.includes(coordinationFailure.entityKey)) setCoordinationFailure(null);
  }, [activeEntityKeys, coordinationFailure]);

  const resolveAction = useCallback(<T,>(
    result: EntityActionLockResult<T>,
    entityKey: string,
    operation: string,
  ): T => {
    try {
      const value = actionLockValue(result, entityKey, operation);
      setCoordinationFailure(null);
      return value;
    } catch (reason) {
      if (reason instanceof EntityActionConflictError) {
        setCoordinationFailure({
          entityKey: reason.entityKey,
          requestedOperation: reason.operation,
          activeOperation: reason.activeOperation,
          message: reason.message,
        });
      }
      throw reason;
    }
  }, []);

  const clearCoordinationError = useCallback(() => setCoordinationFailure(null), []);

  return {
    coordinationError: coordinationFailure?.message ?? "",
    coordinationFailure,
    resolveAction,
    clearCoordinationError,
  };
}
