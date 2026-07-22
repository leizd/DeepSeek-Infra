import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import {
  activeLifecycleMutation,
  createLifecycleId,
  type LifecycleMutationMeta,
} from "../app/mutationLifecycle";

export interface ActionBlocker {
  lifecycleId: string;
  entityKey: string;
  operation: string;
  intentKey?: string;
  source: "local-lock" | "mutation-cache";
}

interface LocalActionLock extends ActionBlocker {
  owner: symbol;
  promise: Promise<unknown>;
}

const localActionLocks = new WeakMap<QueryClient, Map<string, LocalActionLock>>();

function localLocksFor(queryClient: QueryClient): Map<string, LocalActionLock> {
  const existing = localActionLocks.get(queryClient);
  if (existing) return existing;
  const created = new Map<string, LocalActionLock>();
  localActionLocks.set(queryClient, created);
  return created;
}

export function activeLocalAction(
  queryClient: QueryClient,
  predicate: (blocker: ActionBlocker) => boolean,
): ActionBlocker | undefined {
  return [...localLocksFor(queryClient).values()].find(predicate);
}

export type EntityActionLockResult<T> =
  | { status: "executed"; value: T }
  | { status: "deduplicated"; value: T }
  | { status: "conflict"; blocker: ActionBlocker };

export class EntityActionConflictError extends Error {
  readonly requestedEntityKey: string;
  readonly requestedOperation: string;
  readonly blocker: ActionBlocker;
  readonly activeOperation: string;

  constructor(requestedEntityKey: string, requestedOperation: string, blocker: ActionBlocker) {
    super(conflictMessage(blocker.entityKey, requestedOperation, blocker.operation));
    this.name = "EntityActionConflictError";
    this.requestedEntityKey = requestedEntityKey;
    this.requestedOperation = requestedOperation;
    this.blocker = blocker;
    this.activeOperation = blocker.operation;
  }
}

export function lifecycleMutationBlocker(meta: LifecycleMutationMeta): ActionBlocker {
  return {
    lifecycleId: meta.lifecycleId,
    entityKey: meta.entityKey,
    operation: meta.operation,
    intentKey: meta.intentKey,
    source: "mutation-cache",
  };
}

const OPERATION_LABELS: Record<string, string> = {
  create: "创建",
  rename: "重命名",
  remove: "删除",
  upload: "上传",
  update: "保存",
  toggle: "切换状态",
  save: "保存",
  clear: "清空",
};

function conflictMessage(entityKey: string, operation: string, activeOperation: string): string {
  const active = OPERATION_LABELS[activeOperation] ?? activeOperation;
  const requested = OPERATION_LABELS[operation] ?? operation;
  if (entityKey.startsWith("project-binding:")) return `项目技能绑定正在${active}，完成后才能${requested}。`;
  if (entityKey.startsWith("project:")) return `该项目正在${active}，完成后才能${requested}。`;
  if (entityKey.startsWith("skill:")) return `该技能正在${active}，完成后才能${requested}。`;
  if (entityKey.startsWith("memory")) return `长期记忆正在${active}，请稍后再${requested}。`;
  if (entityKey === "project-list:create") return `项目正在${active}，完成后才能${requested}。`;
  if (entityKey === "skill-list:create") return `技能正在${active}，完成后才能${requested}。`;
  return `相关操作正在${active}，完成后才能${requested}。`;
}

export function actionLockValue<T>(
  result: EntityActionLockResult<T>,
  entityKey: string,
  operation: string,
): T {
  if (result.status === "conflict") {
    throw new EntityActionConflictError(entityKey, operation, result.blocker);
  }
  return result.value;
}

export function useEntityActionLocks() {
  const queryClient = useQueryClient();
  const owner = useRef(Symbol("entity-action-lock"));

  return useCallback(
    async <T,>(
      entityKey: string,
      operation: string,
      intentKey: string,
      action: (lifecycleId: string) => Promise<T>,
    ): Promise<EntityActionLockResult<T>> => {
      const locks = localLocksFor(queryClient);
      const existing = locks.get(entityKey);
      if (existing) {
        if (existing.owner !== owner.current || existing.operation !== operation || existing.intentKey !== intentKey) {
          return { status: "conflict", blocker: existing };
        }
        return { status: "deduplicated", value: await existing.promise as T };
      }

      const cached = activeLifecycleMutation(queryClient, (meta) => meta.entityKey === entityKey);
      if (cached) return { status: "conflict", blocker: lifecycleMutationBlocker(cached) };

      const lifecycleId = createLifecycleId();
      const promise = action(lifecycleId);
      locks.set(entityKey, {
        lifecycleId,
        entityKey,
        operation,
        intentKey,
        source: "local-lock",
        owner: owner.current,
        promise,
      });
      try {
        return { status: "executed", value: await promise };
      } finally {
        if (locks.get(entityKey)?.promise === promise) locks.delete(entityKey);
      }
    },
    [queryClient],
  );
}
