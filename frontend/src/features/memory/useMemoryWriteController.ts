import { useCallback } from "react";
import { useMutationState, useQueryClient } from "@tanstack/react-query";

import { addMemory, MemoryConflictError, type MemoryEntry } from "../../api/memoryApi";
import { MEMORIES_QUERY_KEY } from "../../app/queryKeys";
import { mutationKeys, ownsMutationKey } from "../../app/mutationKeys";
import { stableIntentKey } from "../../app/mutationIntents";
import {
  isLifecycleMutationMeta,
  isMutationActive,
  removeFailedMutations,
  type LifecycleMutationMeta,
  useMutationActivity,
} from "../../app/mutationLifecycle";
import { latestUnresolvedLifecycleError, type LifecycleMutationSnapshot } from "../../app/mutationErrors";
import { useActionCoordination } from "../../shared/useActionCoordination";
import { useMemoryWriteBarrier } from "./useMemoryWriteBarrier";

export interface MemorySaveResult {
  saved: boolean;
  conflicts: readonly { id: string; content: string; reason: string }[];
}

export interface MemorySaveInput {
  content: string;
  category?: string;
  scope?: string;
  replaceIds?: readonly string[];
}

type MemorySaveOutcome =
  | { saved: true; entry: MemoryEntry; conflicts: readonly [] }
  | { saved: false; conflicts: readonly { id: string; content: string; reason: string }[] };

export interface MemoryWriteController {
  clearing: boolean;
  saving: boolean;
  hasPendingWrites: boolean;
  error: string;
  save(input: MemorySaveInput): Promise<MemorySaveResult>;
  recoverWrites(): void;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function memorySaveIntent(input: MemorySaveInput): string {
  return stableIntentKey({
    content: input.content.trim(),
    category: input.category ?? "fact",
    scope: input.scope ?? "global",
    replaceIds: [...(input.replaceIds ?? [])].sort(),
  });
}

function memoryMutationMeta(
  lifecycleId: string,
  entityKey: string,
  operation: string,
  intentKey: string,
): LifecycleMutationMeta {
  return { owner: "memory-list", lifecycleId, entityKey, operation, intentKey };
}

export function useMemoryWriteController(): MemoryWriteController {
  const queryClient = useQueryClient();
  const { runWrite } = useMemoryWriteBarrier();
  const { coordinationError, resolveAction, clearCoordinationError } = useActionCoordination();
  const clearActivity = useMutationActivity(mutationKeys.memoryList.clear);
  const saveActivity = useMutationActivity(mutationKeys.memoryList.save);
  const pendingWriteMutations = useMutationState<number>({
    filters: {
      predicate: (mutation) => isMutationActive(mutation.state) && ownsMutationKey(
        mutation.options.mutationKey,
        [mutationKeys.memoryList.save, mutationKeys.memoryList.remove],
      ),
    },
    select: () => 1,
  });

  const save = useCallback(
    async (input: MemorySaveInput): Promise<MemorySaveResult> => {
      const intentKey = memorySaveIntent(input);
      const entityKey = `memory-save:${intentKey}`;
      const operation = "save";
      const result = await runWrite(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.memoryList.save,
          meta: memoryMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: MEMORIES_QUERY_KEY });
          },
          mutationFn: async (value: MemorySaveInput): Promise<MemorySaveOutcome> => {
            try {
              return { saved: true, entry: await addMemory(value), conflicts: [] };
            } catch (reason) {
              if (reason instanceof MemoryConflictError) return { saved: false, conflicts: reason.conflicts };
              throw reason;
            }
          },
          onSuccess: (outcome, value) => {
            if (!outcome.saved) return;
            queryClient.setQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY, (current) => {
              if (!current) return current;
              const replaced = new Set(value.replaceIds ?? []);
              return [...current.filter((entry) => entry.id !== outcome.entry.id && !replaced.has(entry.id)), outcome.entry];
            });
          },
          onSettled: (outcome) => {
            if (outcome?.saved) void queryClient.invalidateQueries({ queryKey: MEMORIES_QUERY_KEY });
          },
        });
        return mutation.execute(input);
      });
      const outcome = resolveAction(result, entityKey, operation);
      return { saved: outcome.saved, conflicts: outcome.conflicts };
    },
    [queryClient, resolveAction, runWrite],
  );

  const mutationErrors = useMutationState<LifecycleMutationSnapshot>({
    filters: { predicate: (mutation) => ownsMutationKey(mutation.options.mutationKey, [mutationKeys.memoryList.save]) },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
      meta: isLifecycleMutationMeta(mutation.options.meta) ? mutation.options.meta : undefined,
    }),
  });
  const firstError = latestUnresolvedLifecycleError(mutationErrors);

  return {
    clearing: clearActivity.active,
    saving: saveActivity.active,
    hasPendingWrites: pendingWriteMutations.length > 0,
    error: coordinationError || (firstError ? errorText(firstError, "记忆保存失败") : ""),
    save,
    recoverWrites: () => {
      clearCoordinationError();
      removeFailedMutations(queryClient, [mutationKeys.memoryList.save]);
    },
  };
}
