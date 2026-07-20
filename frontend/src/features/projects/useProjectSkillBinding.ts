import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchProjectSkillBinding,
  saveProjectSkillBinding,
  type ProjectSkillBinding,
} from "../../api/skillsApi";
import { projectSkillBindingQueryKey } from "../../app/queryKeys";

export interface ProjectSkillBindingController {
  binding: ProjectSkillBinding | undefined;
  loading: boolean;
  refreshing: boolean;
  saving: boolean;
  error: unknown;
  save(binding: ProjectSkillBinding): Promise<ProjectSkillBinding>;
  retry(): void;
}

export function useProjectSkillBinding(projectId: string): ProjectSkillBindingController {
  const queryClient = useQueryClient();
  const queryKey = projectSkillBindingQueryKey(projectId);

  const bindingQuery = useQuery<ProjectSkillBinding>({
    queryKey,
    enabled: Boolean(projectId),
    queryFn: ({ signal }) => fetchProjectSkillBinding(projectId, { signal }),
  });

  const saveMutation = useMutation({
    mutationKey: [...queryKey, "save"],
    scope: { id: `project-skill-binding:${projectId}` },
    mutationFn: (binding: ProjectSkillBinding) =>
      saveProjectSkillBinding(projectId, {
        enabledSkills: binding.enabledSkills,
        defaultSkill: binding.defaultSkill,
      }),
    onSuccess: (binding) => {
      queryClient.setQueryData(queryKey, binding);
      void queryClient.invalidateQueries({ queryKey });
    },
  });

  return {
    binding: bindingQuery.data,
    loading: bindingQuery.isLoading,
    refreshing: bindingQuery.isFetching && !bindingQuery.isLoading,
    saving: saveMutation.isPending || saveMutation.isPaused,
    error: bindingQuery.error ?? saveMutation.error,
    save: saveMutation.mutateAsync,
    retry: () => void bindingQuery.refetch(),
  };
}
