import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { activeLifecycleMutation } from "../app/mutationLifecycle";

export type EntityActionLockResult<T> =
  | { status: "executed"; value: T }
  | { status: "deduplicated"; value: T }
  | { status: "conflict"; activeOperation: string };

export class EntityActionConflictError extends Error {
  readonly entityKey: string;
  readonly operation: string;
  readonly activeOperation: string;

  constructor(entityKey: string, operation: string, activeOperation: string) {
    super(conflictMessage(entityKey, operation, activeOperation));
    this.name = "EntityActionConflictError";
    this.entityKey = entityKey;
    this.operation = operation;
    this.activeOperation = activeOperation;
  }
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
    throw new EntityActionConflictError(entityKey, operation, result.activeOperation);
  }
  return result.value;
}

export function useEntityActionLocks() {
  const queryClient = useQueryClient();
  const locks = useRef(new Map<string, { operation: string; intentKey: string; promise: Promise<unknown> }>());

  return useCallback(
    async <T,>(
      entityKey: string,
      operation: string,
      intentKey: string,
      action: () => Promise<T>,
    ): Promise<EntityActionLockResult<T>> => {
      const existing = locks.current.get(entityKey);
      if (existing) {
        if (existing.operation !== operation || existing.intentKey !== intentKey) {
          return { status: "conflict", activeOperation: existing.operation };
        }
        return { status: "deduplicated", value: await existing.promise as T };
      }

      const cached = activeLifecycleMutation(queryClient, (meta) => meta.entityKey === entityKey);
      if (cached) return { status: "conflict", activeOperation: cached.operation };

      const promise = action();
      locks.current.set(entityKey, { operation, intentKey, promise });
      try {
        return { status: "executed", value: await promise };
      } finally {
        if (locks.current.get(entityKey)?.promise === promise) locks.current.delete(entityKey);
      }
    },
    [queryClient],
  );
}
