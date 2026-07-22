import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchProjectSkillBinding,
  saveProjectSkillBinding,
  type ProjectSkillBinding,
} from "../../api/skillsApi";
import { mutationKeys } from "../../app/mutationKeys";
import { projectSkillBindingQueryKey } from "../../app/queryKeys";

export type BindingErrorKind = "load" | "save" | null;

export interface ProjectSkillBindingController {
  binding: ProjectSkillBinding | undefined;
  loading: boolean;
  refreshing: boolean;
  saving: boolean;
  error: unknown;
  errorKind: BindingErrorKind;
  save(binding: ProjectSkillBinding): Promise<ProjectSkillBinding>;
  retry(): Promise<void>;
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
    mutationKey: mutationKeys.projectBinding.save(projectId),
    scope: { id: `project-skill-binding:${projectId}` },
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey });
    },
    mutationFn: (binding: ProjectSkillBinding) =>
      saveProjectSkillBinding(projectId, {
        enabledSkills: binding.enabledSkills,
        defaultSkill: binding.defaultSkill,
      }),
    onSuccess: (binding) => {
      queryClient.setQueryData(queryKey, binding);
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey }),
  });

  async function retry(): Promise<void> {
    if (saveMutation.isError && saveMutation.variables) {
      const desiredBinding = saveMutation.variables;
      saveMutation.reset();
      await saveMutation.mutateAsync(desiredBinding);
      return;
    }
    await bindingQuery.refetch();
  }

  const error = bindingQuery.error ?? saveMutation.error;
  const errorKind: BindingErrorKind = bindingQuery.error ? "load" : saveMutation.error ? "save" : null;

  return {
    binding: bindingQuery.data,
    loading: bindingQuery.isLoading,
    refreshing: bindingQuery.isFetching && !bindingQuery.isLoading,
    saving: saveMutation.isPending || saveMutation.isPaused,
    error,
    errorKind,
    save: saveMutation.mutateAsync,
    retry,
  };
}
