import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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
import { latestMutationError } from "../../app/mutationErrors";

export { SKILLS_QUERY_KEY };

export interface SkillController {
  skills: readonly Skill[];
  loading: boolean;
  refreshing: boolean;
  creating: boolean;
  updatingSkillId: string | null;
  togglingSkillId: string | null;
  removingSkillId: string | null;
  error: string;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  toggle(skill: Skill): Promise<void>;
  remove(skillId: string): Promise<void>;
  create(draft: SimpleSkillDraft): Promise<void>;
  update(draft: SimpleSkillDraft & { skillId: string }): Promise<void>;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function useSkillController(): SkillController {
  const queryClient = useQueryClient();
  const skillsQuery = useQuery<Skill[]>({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: ({ signal }) => listSkills({ signal }),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY }),
    [queryClient],
  );

  const toggleMutation = useMutation({
    mutationFn: (skill: Skill) => setSkillDisabled(skill.skillId, !skill.disabled),
    onSuccess: (_result, skill) => {
      queryClient.setQueryData<Skill[]>(SKILLS_QUERY_KEY, (current) =>
        (current ?? []).map((item) => (item.skillId === skill.skillId ? { ...item, disabled: !skill.disabled } : item)),
      );
      void invalidate();
    },
  });

  const removeMutation = useMutation({
    mutationFn: (skillId: string) => deleteSkill(skillId),
    onSuccess: (_result, skillId) => {
      queryClient.setQueryData<Skill[]>(SKILLS_QUERY_KEY, (current) =>
        (current ?? []).filter((item) => item.skillId !== skillId),
      );
      void invalidate();
    },
  });

  const createMutation = useMutation({
    mutationFn: (draft: SimpleSkillDraft) => createSkill(draft),
    onSuccess: () => void invalidate(),
  });

  const updateMutation = useMutation({
    mutationFn: (draft: SimpleSkillDraft & { skillId: string }) => updateSkillPrompt(draft),
    onSuccess: () => void invalidate(),
  });

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const recover = useCallback(async () => {
    if (toggleMutation.isError) toggleMutation.reset();
    if (removeMutation.isError) removeMutation.reset();
    if (createMutation.isError) createMutation.reset();
    if (updateMutation.isError) updateMutation.reset();
    await invalidate();
  }, [createMutation, invalidate, removeMutation, toggleMutation, updateMutation]);

  const toggle = useCallback(
    async (skill: Skill) => {
      await toggleMutation.mutateAsync(skill);
    },
    [toggleMutation],
  );
  const remove = useCallback(
    async (skillId: string) => {
      await removeMutation.mutateAsync(skillId);
    },
    [removeMutation],
  );
  const create = useCallback(
    async (draft: SimpleSkillDraft) => {
      await createMutation.mutateAsync(draft);
    },
    [createMutation],
  );
  const update = useCallback(
    async (draft: SimpleSkillDraft & { skillId: string }) => {
      await updateMutation.mutateAsync(draft);
    },
    [updateMutation],
  );

  const firstError =
    skillsQuery.error ?? latestMutationError(toggleMutation, removeMutation, createMutation, updateMutation);

  return {
    skills: skillsQuery.data ?? [],
    loading: skillsQuery.isLoading,
    refreshing: skillsQuery.isFetching && !skillsQuery.isLoading,
    creating: createMutation.isPending,
    updatingSkillId: updateMutation.isPending ? (updateMutation.variables?.skillId ?? null) : null,
    togglingSkillId: toggleMutation.isPending ? (toggleMutation.variables?.skillId ?? null) : null,
    removingSkillId: removeMutation.isPending ? (removeMutation.variables ?? null) : null,
    error: firstError ? errorText(firstError, "技能操作失败") : "",
    refresh,
    recover,
    toggle,
    remove,
    create,
    update,
  };
}
