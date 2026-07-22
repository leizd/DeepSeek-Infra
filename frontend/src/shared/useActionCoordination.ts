import { useMutationState } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import {
  isLifecycleMutationMeta,
  isMutationActive,
} from "../app/mutationLifecycle";

import {
  actionLockValue,
  EntityActionConflictError,
  type ActionBlocker,
  type EntityActionLockResult,
} from "./useEntityActionLocks";

export interface CoordinationFailure {
  requestedEntityKey: string;
  requestedOperation: string;
  blocker: ActionBlocker;
  message: string;
}

export function useActionCoordination() {
  const [coordinationFailure, setCoordinationFailure] = useState<CoordinationFailure | null>(null);
  const activeLifecycleIds = useMutationState<string>({
    filters: {
      predicate: (mutation) =>
        isMutationActive(mutation.state) && isLifecycleMutationMeta(mutation.options.meta),
    },
    select: (mutation) => isLifecycleMutationMeta(mutation.options.meta)
      ? mutation.options.meta.lifecycleId
      : "",
  });

  useEffect(() => {
    if (!coordinationFailure) return;
    if (!activeLifecycleIds.includes(coordinationFailure.blocker.lifecycleId)) setCoordinationFailure(null);
  }, [activeLifecycleIds, coordinationFailure]);

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
          requestedEntityKey: reason.requestedEntityKey,
          requestedOperation: reason.requestedOperation,
          blocker: reason.blocker,
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
