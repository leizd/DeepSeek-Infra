import { useCallback, useMemo } from "react";
import { useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createSkill,
  deleteSkill,
  listSkills,
  setSkillDisabled,
  updateSkillPrompt,
  type SimpleSkillDraft,
  type Skill,
} from "../../api/skillsApi";
import { SKILLS_QUERY_KEY } from "../../app/queryKeys";
import { mutationKeys, ownsMutationKey, SKILL_LIST_MUTATION_KEYS } from "../../app/mutationKeys";
import { skillDraftIntent } from "../../app/mutationIntents";
import {
  isLifecycleMutationMeta,
  isMutationActive,
  removeFailedMutations,
  type LifecycleMutationMeta,
  useMutationActivity,
} from "../../app/mutationLifecycle";
import {
  latestUnresolvedLifecycleError,
  type LifecycleMutationSnapshot,
} from "../../app/mutationErrors";
import { useActionCoordination } from "../../shared/useActionCoordination";
import { useEntityActionLocks } from "../../shared/useEntityActionLocks";

export { SKILLS_QUERY_KEY };

export interface SkillController {
  skills: readonly Skill[];
  loading: boolean;
  refreshing: boolean;
  creating: boolean;
  error: string;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  toggle(skill: Skill): Promise<void>;
  remove(skillId: string): Promise<void>;
  create(draft: SimpleSkillDraft): Promise<void>;
  update(draft: SimpleSkillDraft & { skillId: string }): Promise<void>;
  isUpdatingSkill(skillId: string): boolean;
  isTogglingSkill(skillId: string): boolean;
  isRemovingSkill(skillId: string): boolean;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function skillMutationMeta(
  lifecycleId: string,
  entityKey: string,
  operation: string,
  intentKey: string,
): LifecycleMutationMeta {
  return { owner: "skill-list", lifecycleId, entityKey, operation, intentKey };
}

export function useSkillController(): SkillController {
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();
  const { coordinationError, resolveAction, clearCoordinationError } = useActionCoordination();
  const skillsQuery = useQuery<Skill[]>({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: ({ signal }) => listSkills({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY }),
    [queryClient],
  );

  const createActivity = useMutationActivity(mutationKeys.skillList.create);

  const updatingSkillIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.skillList.update,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as { skillId?: string } | undefined)?.skillId ?? "",
  });
  const togglingSkillIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.skillList.toggle,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as Skill | undefined)?.skillId ?? "",
  });
  const removingSkillIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.skillList.remove,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as string | undefined) ?? "",
  });
  const updatingSkillIdSet = useMemo(() => new Set(updatingSkillIds), [updatingSkillIds]);
  const togglingSkillIdSet = useMemo(() => new Set(togglingSkillIds), [togglingSkillIds]);
  const removingSkillIdSet = useMemo(() => new Set(removingSkillIds), [removingSkillIds]);
  const isUpdatingSkill = useCallback((skillId: string) => updatingSkillIdSet.has(skillId), [updatingSkillIdSet]);
  const isTogglingSkill = useCallback((skillId: string) => togglingSkillIdSet.has(skillId), [togglingSkillIdSet]);
  const isRemovingSkill = useCallback((skillId: string) => removingSkillIdSet.has(skillId), [removingSkillIdSet]);

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const recover = useCallback(async () => {
    clearCoordinationError();
    removeFailedMutations(queryClient, SKILL_LIST_MUTATION_KEYS);
    await queryClient.refetchQueries({ queryKey: SKILLS_QUERY_KEY, type: "active" });
  }, [clearCoordinationError, queryClient]);

  const toggle = useCallback(
    async (skill: Skill) => {
      const entityKey = `skill:${skill.skillId}`;
      const operation = "toggle";
      const intentKey = String(!skill.disabled);
      const result = await runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.skillList.toggle,
          meta: skillMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
          },
          mutationFn: (s: Skill) => setSkillDisabled(s.skillId, !s.disabled),
          onSuccess: (_result, s) => {
            queryClient.setQueryData<Skill[]>(SKILLS_QUERY_KEY, (current) =>
              (current ?? []).map((item) => (item.skillId === s.skillId ? { ...item, disabled: !s.disabled } : item)),
            );
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute(skill);
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runEntityAction],
  );
  const remove = useCallback(
    async (skillId: string) => {
      const entityKey = `skill:${skillId}`;
      const operation = "remove";
      const intentKey = skillId;
      const result = await runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.skillList.remove,
          meta: skillMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
          },
          mutationFn: (id: string) => deleteSkill(id),
          onSuccess: (_result, id) => {
            queryClient.setQueryData<Skill[]>(SKILLS_QUERY_KEY, (current) =>
              (current ?? []).filter((item) => item.skillId !== id),
            );
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute(skillId);
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runEntityAction],
  );
  const create = useCallback(
    async (draft: SimpleSkillDraft) => {
      const entityKey = "skill-list:create";
      const operation = "create";
      const intentKey = skillDraftIntent(draft);
      const result = await runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.skillList.create,
          meta: skillMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
          },
          mutationFn: (value: SimpleSkillDraft) => createSkill(value),
          onSettled: () => void invalidate(),
        });
        return mutation.execute(draft);
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runEntityAction],
  );
  const update = useCallback(
    async (draft: SimpleSkillDraft & { skillId: string }) => {
      const entityKey = `skill:${draft.skillId}`;
      const operation = "update";
      const intentKey = skillDraftIntent(draft);
      const result = await runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.skillList.update,
          meta: skillMutationMeta(lifecycleId, entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
          },
          mutationFn: (value: SimpleSkillDraft & { skillId: string }) => updateSkillPrompt(value),
          onSettled: () => void invalidate(),
        });
        return mutation.execute(draft);
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runEntityAction],
  );

  const mutationErrors = useMutationState<LifecycleMutationSnapshot>({
    filters: { predicate: (mutation) => ownsMutationKey(mutation.options.mutationKey, SKILL_LIST_MUTATION_KEYS) },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
      meta: isLifecycleMutationMeta(mutation.options.meta) ? mutation.options.meta : undefined,
    }),
  });

  const firstError =
    skillsQuery.error ?? latestUnresolvedLifecycleError(mutationErrors);

  return {
    skills: skillsQuery.data ?? [],
    loading: skillsQuery.isLoading,
    refreshing: skillsQuery.isFetching && !skillsQuery.isLoading,
    creating: createActivity.active,
    error: coordinationError || (firstError ? errorText(firstError, "技能操作失败") : ""),
    refresh,
    recover,
    toggle,
    remove,
    create,
    update,
    isUpdatingSkill,
    isTogglingSkill,
    isRemovingSkill,
  };
}
