import { useCallback, useMemo } from "react";
import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

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
import { removeFailedMutations } from "../../app/mutationLifecycle";
import { latestCacheMutationError, type MutationStateSnapshot } from "../../app/mutationErrors";
import { actionLockValue, useEntityActionLocks } from "../../shared/useEntityActionLocks";

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

export function useSkillController(): SkillController {
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();
  const skillsQuery = useQuery<Skill[]>({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: ({ signal }) => listSkills({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY }),
    [queryClient],
  );

  const createMutation = useMutation({
    mutationKey: mutationKeys.skillList.create,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
    },
    mutationFn: (draft: SimpleSkillDraft) => createSkill(draft),
    onSettled: () => void invalidate(),
  });

  const updateMutation = useMutation({
    mutationKey: mutationKeys.skillList.update,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
    },
    mutationFn: (draft: SimpleSkillDraft & { skillId: string }) => updateSkillPrompt(draft),
    onSettled: () => void invalidate(),
  });

  const updatingSkillIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.skillList.update, exact: true, status: "pending" },
    select: (mutation) => (mutation.state.variables as { skillId?: string } | undefined)?.skillId ?? "",
  });
  const togglingSkillIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.skillList.toggle, exact: true, status: "pending" },
    select: (mutation) => (mutation.state.variables as Skill | undefined)?.skillId ?? "",
  });
  const removingSkillIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.skillList.remove, exact: true, status: "pending" },
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
    removeFailedMutations(queryClient, SKILL_LIST_MUTATION_KEYS);
    await queryClient.refetchQueries({ queryKey: SKILLS_QUERY_KEY, type: "active" });
  }, [queryClient]);

  const toggle = useCallback(
    async (skill: Skill) => {
      const entityKey = `skill:${skill.skillId}`;
      const result = await runEntityAction(entityKey, "toggle", async () => {
        await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.skillList.toggle,
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
      actionLockValue(result, entityKey, "toggle");
    },
    [invalidate, queryClient, runEntityAction],
  );
  const remove = useCallback(
    async (skillId: string) => {
      const entityKey = `skill:${skillId}`;
      const result = await runEntityAction(entityKey, "remove", async () => {
        await queryClient.cancelQueries({ queryKey: SKILLS_QUERY_KEY });
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.skillList.remove,
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
      actionLockValue(result, entityKey, "remove");
    },
    [invalidate, queryClient, runEntityAction],
  );
  const create = useCallback(
    async (draft: SimpleSkillDraft) => {
      await createMutation.mutateAsync(draft);
    },
    [createMutation],
  );
  const update = useCallback(
    async (draft: SimpleSkillDraft & { skillId: string }) => {
      const entityKey = `skill:${draft.skillId}`;
      const result = await runEntityAction(entityKey, "update", () => updateMutation.mutateAsync(draft));
      actionLockValue(result, entityKey, "update");
    },
    [runEntityAction, updateMutation],
  );

  const mutationErrors = useMutationState<MutationStateSnapshot>({
    filters: { predicate: (mutation) => ownsMutationKey(mutation.options.mutationKey, SKILL_LIST_MUTATION_KEYS) },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
    }),
  });

  const firstError =
    skillsQuery.error ?? latestCacheMutationError(mutationErrors);

  return {
    skills: skillsQuery.data ?? [],
    loading: skillsQuery.isLoading,
    refreshing: skillsQuery.isFetching && !skillsQuery.isLoading,
    creating: createMutation.isPending,
    error: firstError ? errorText(firstError, "技能操作失败") : "",
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
