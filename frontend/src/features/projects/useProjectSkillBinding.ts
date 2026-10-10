import { useCallback, useMemo } from "react";
import { useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchProjectSkillBinding,
  saveProjectSkillBinding,
  type ProjectSkillBinding,
} from "../../api/skillsApi";
import { mutationKeys } from "../../app/mutationKeys";
import { stableIntentKey } from "../../app/mutationIntents";
import {
  activeLifecycleMutation,
  isLifecycleMutationMeta,
  isMutationActive,
  type LifecycleMutationMeta,
  useMutationActivity,
} from "../../app/mutationLifecycle";
import { projectSkillBindingQueryKey } from "../../app/queryKeys";
import { useActionCoordination } from "../../shared/useActionCoordination";
import {
  activeLocalAction,
  lifecycleMutationBlocker,
  useEntityActionLocks,
  type EntityActionLockResult,
} from "../../shared/useEntityActionLocks";

export type BindingErrorKind = "load" | "save" | null;

export interface ProjectSkillBindingController {
  binding: ProjectSkillBinding | undefined;
  loading: boolean;
  refreshing: boolean;
  saving: boolean;
  projectRemoving: boolean;
  error: unknown;
  errorKind: BindingErrorKind;
  save(binding: ProjectSkillBinding): Promise<ProjectSkillBinding>;
  retry(): Promise<void>;
}

interface BindingMutationSnapshot {
  status: string;
  error: unknown;
  variables: unknown;
  submittedAt: number;
}

function bindingIntent(binding: ProjectSkillBinding): string {
  return stableIntentKey({
    enabledSkills: [...binding.enabledSkills].sort(),
    defaultSkill: binding.defaultSkill,
  });
}

function latestBindingMutation(
  mutations: readonly BindingMutationSnapshot[],
): BindingMutationSnapshot | undefined {
  return mutations.reduce<BindingMutationSnapshot | undefined>(
    (current, mutation) => !current || mutation.submittedAt > current.submittedAt ? mutation : current,
    undefined,
  );
}

export function useProjectSkillBinding(projectId: string): ProjectSkillBindingController {
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();
  const { coordinationError, resolveAction, clearCoordinationError } = useActionCoordination();
  const queryKey = projectSkillBindingQueryKey(projectId);
  const mutationKey = mutationKeys.projectBinding.save(projectId);
  const entityKey = `project-binding:${projectId}`;

  const bindingQuery = useQuery<ProjectSkillBinding>({
    queryKey,
    enabled: Boolean(projectId),
    queryFn: ({ signal }) => fetchProjectSkillBinding(projectId, { signal }),
  });

  const saveActivity = useMutationActivity(mutationKey);
  const saveMutations = useMutationState<BindingMutationSnapshot>({
    filters: { mutationKey, exact: true },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      variables: mutation.state.variables,
      submittedAt: mutation.state.submittedAt,
    }),
  });
  const latestSave = useMemo(() => latestBindingMutation(saveMutations), [saveMutations]);
  const removingProjectIds = useMutationState<string>({
    filters: {
      predicate: (mutation) =>
        isMutationActive(mutation.state)
        && isLifecycleMutationMeta(mutation.options.meta)
        && mutation.options.meta.owner === "project-list"
        && mutation.options.meta.operation === "remove",
    },
    select: (mutation) => isLifecycleMutationMeta(mutation.options.meta)
      ? mutation.options.meta.entityKey.slice("project:".length)
      : "",
  });
  const projectRemoving = removingProjectIds.includes(projectId);

  const save = useCallback(async (binding: ProjectSkillBinding): Promise<ProjectSkillBinding> => {
    const operation = "save";
    const intentKey = bindingIntent(binding);
    const localRemovalBlocker = activeLocalAction(
      queryClient,
      (blocker) => blocker.entityKey === `project:${projectId}` && blocker.operation === "remove",
    );
    const removalBlocker = activeLifecycleMutation(
      queryClient,
      (meta) => meta.owner === "project-list"
        && meta.entityKey === `project:${projectId}`
        && meta.operation === "remove",
    );
    const result: EntityActionLockResult<ProjectSkillBinding> = localRemovalBlocker || removalBlocker
      ? {
          status: "conflict",
          blocker: localRemovalBlocker ?? lifecycleMutationBlocker(removalBlocker!),
        }
      : await runEntityAction(entityKey, operation, intentKey, async (lifecycleId) => {
      const meta: LifecycleMutationMeta = {
        owner: "project-binding",
        lifecycleId,
        entityKey,
        operation,
        intentKey,
      };
      const mutation = queryClient.getMutationCache().build(queryClient, {
        mutationKey,
        meta,
        scope: { id: `project-skill-binding:${projectId}` },
        onMutate: async () => {
          await queryClient.cancelQueries({ queryKey });
        },
        mutationFn: (value: ProjectSkillBinding) =>
          saveProjectSkillBinding(projectId, {
            enabledSkills: value.enabledSkills,
            defaultSkill: value.defaultSkill,
          }),
        onSuccess: (savedBinding) => {
          queryClient.setQueryData(queryKey, savedBinding);
        },
        onSettled: () => void queryClient.invalidateQueries({ queryKey }),
      });
      return mutation.execute(binding);
      });
    return resolveAction(result, entityKey, operation);
  }, [entityKey, mutationKey, projectId, queryClient, queryKey, resolveAction, runEntityAction]);

  async function retry(): Promise<void> {
    clearCoordinationError();
    const latestMutation = queryClient.getMutationCache().findAll({ mutationKey, exact: true })
      .sort((left, right) => right.state.submittedAt - left.state.submittedAt)[0];
    if (latestMutation?.state.status === "error" && latestMutation.state.variables) {
      await save(latestMutation.state.variables as ProjectSkillBinding);
      return;
    }
    await bindingQuery.refetch();
  }

  const latestSaveError = latestSave?.status === "error" ? latestSave.error : null;
  const error = coordinationError || bindingQuery.error || latestSaveError;
  const errorKind: BindingErrorKind = bindingQuery.error ? "load" : error ? "save" : null;

  return {
    binding: bindingQuery.data,
    loading: bindingQuery.isLoading,
    refreshing: bindingQuery.isFetching && !bindingQuery.isLoading,
    saving: saveActivity.active,
    projectRemoving,
    error,
    errorKind,
    save,
    retry,
  };
}
