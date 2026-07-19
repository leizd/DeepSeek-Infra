import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createSkill,
  deleteSkill,
  fetchProjectSkillBinding,
  listSkills,
  saveProjectSkillBinding,
  setSkillDisabled,
  updateSkillPrompt,
  type ProjectSkillBinding,
  type SimpleSkillDraft,
  type Skill,
} from "../../api/skillsApi";

export const SKILLS_QUERY_KEY = ["skills"] as const;

export function projectSkillBindingQueryKey(projectId: string) {
  return ["projects", projectId, "skillBinding"] as const;
}

export interface SkillController {
  skills: readonly Skill[];
  loading: boolean;
  error: string;
  refresh(): Promise<void>;
  toggle(skill: Skill): Promise<void>;
  remove(skillId: string): Promise<void>;
  create(draft: SimpleSkillDraft): Promise<void>;
  update(draft: SimpleSkillDraft & { skillId: string }): Promise<void>;
  loadBinding(projectId: string): Promise<ProjectSkillBinding>;
  saveBinding(projectId: string, enabledSkills: readonly string[], defaultSkill: string): Promise<void>;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function useSkillController(): SkillController {
  const queryClient = useQueryClient();
  const skillsQuery = useQuery<Skill[]>({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: () => listSkills(),
  });

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY }),
    [queryClient],
  );

  const toggleMutation = useMutation({
    mutationFn: (skill: Skill) => setSkillDisabled(skill.skillId, !skill.disabled),
    onSuccess: () => void invalidate(),
  });

  const removeMutation = useMutation({
    mutationFn: (skillId: string) => deleteSkill(skillId),
    onSuccess: () => void invalidate(),
  });

  const createMutation = useMutation({
    mutationFn: (draft: SimpleSkillDraft) => createSkill(draft),
    onSuccess: () => void invalidate(),
  });

  const updateMutation = useMutation({
    mutationFn: (draft: SimpleSkillDraft & { skillId: string }) => updateSkillPrompt(draft),
    onSuccess: () => void invalidate(),
  });

  const bindingMutation = useMutation({
    mutationFn: ({ projectId, enabledSkills, defaultSkill }: { projectId: string; enabledSkills: readonly string[]; defaultSkill: string }) =>
      saveProjectSkillBinding(projectId, { enabledSkills, defaultSkill }),
    onSuccess: (_binding, variables) =>
      queryClient.invalidateQueries({ queryKey: projectSkillBindingQueryKey(variables.projectId) }),
  });

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

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

  const loadBinding = useCallback(
    (projectId: string) =>
      queryClient.fetchQuery({
        queryKey: projectSkillBindingQueryKey(projectId),
        queryFn: () => fetchProjectSkillBinding(projectId),
      }),
    [queryClient],
  );

  const saveBinding = useCallback(
    async (projectId: string, enabledSkills: readonly string[], defaultSkill: string) => {
      await bindingMutation.mutateAsync({ projectId, enabledSkills, defaultSkill });
    },
    [bindingMutation],
  );

  const firstError =
    skillsQuery.error ?? toggleMutation.error ?? removeMutation.error ?? createMutation.error ?? updateMutation.error;

  return {
    skills: skillsQuery.data ?? [],
    loading: skillsQuery.isLoading,
    error: firstError ? errorText(firstError, "技能操作失败") : "",
    refresh,
    toggle,
    remove,
    create,
    update,
    loadBinding,
    saveBinding,
  };
}
