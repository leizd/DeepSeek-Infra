import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createProject,
  deleteProject,
  listProjects,
  renameProject,
  uploadProjectFiles,
  type Project,
} from "../../api/projectsApi";
import type { Attachment } from "../../domain/chat/types";
import {
  mutationKeys,
  ownsMutationKey,
  PROJECT_LIST_MUTATION_KEYS,
} from "../../app/mutationKeys";
import {
  isMutationActive,
  removeFailedMutations,
  type LifecycleMutationMeta,
  useMutationActivity,
} from "../../app/mutationLifecycle";
import { PROJECTS_QUERY_KEY } from "../../app/queryKeys";
import { latestCacheMutationError, type MutationStateSnapshot } from "../../app/mutationErrors";
import { useSettings } from "../../contexts/SettingsContext";
import { useActionCoordination } from "../../shared/useActionCoordination";
import { useEntityActionLocks } from "../../shared/useEntityActionLocks";

const ACTIVE_PROJECT_KEY = "deepseek-infra.active-project";

export { PROJECTS_QUERY_KEY };

export interface ProjectChatContext {
  projectId?: string;
  projectAttachments: Attachment[];
  memoryScope?: string;
}

export function projectDocumentsToAttachments(project: Project): Attachment[] {
  return project.documents.map((document) => ({
    name: document.name,
    type: document.type,
    size: document.size,
    kind: document.kind,
    fileId: document.fileId,
    projectId: document.projectId || project.id,
    sourceAvailable: document.sourceAvailable,
    preview: document.preview,
    pageCount: document.pageCount,
    charCount: document.charCount,
    chunkCount: document.chunkCount,
    chunked: document.chunked,
  }));
}

export interface ProjectController {
  projects: readonly Project[];
  activeProjectId: string;
  activeProject: Project | null;
  loading: boolean;
  refreshing: boolean;
  uploading: boolean;
  creating: boolean;
  error: string;
  refresh(): Promise<void>;
  recover(): Promise<void>;
  create(name: string): Promise<void>;
  remove(projectId: string): Promise<void>;
  rename(projectId: string, name: string): Promise<void>;
  isRenamingProject(projectId: string): boolean;
  isRemovingProject(projectId: string): boolean;
  isUploadingProject(projectId: string): boolean;
  setActive(projectId: string): void;
  uploadDocuments(files: Iterable<File>): Promise<void>;
  chatContext(): ProjectChatContext;
}

interface ProjectUploadVariables {
  projectId: string;
  files: File[];
  apiKey?: string;
}

function storedActiveProject(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ACTIVE_PROJECT_KEY) ?? "";
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function uploadIntent(files: readonly File[]): string {
  return files
    .map((file) => `${file.name}:${file.size}:${file.lastModified}`)
    .sort()
    .join("|");
}

function projectMutationMeta(
  entityKey: string,
  operation: string,
  intentKey: string,
): LifecycleMutationMeta {
  return { owner: "project-list", entityKey, operation, intentKey };
}

export function useProjectController(): ProjectController {
  const settings = useSettings();
  const queryClient = useQueryClient();
  const runEntityAction = useEntityActionLocks();
  const { coordinationError, resolveAction, clearCoordinationError } = useActionCoordination();
  const [activeProjectId, setActiveProjectId] = useState(storedActiveProject);

  const projectsQuery = useQuery<Project[]>({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: ({ signal }) => listProjects({ signal }),
  });
  const projects: readonly Project[] = useMemo(() => projectsQuery.data ?? [], [projectsQuery.data]);

  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
    [queryClient],
  );

  const setActive = useCallback((projectId: string) => {
    setActiveProjectId(projectId);
    if (typeof window !== "undefined") {
      if (projectId) window.localStorage.setItem(ACTIVE_PROJECT_KEY, projectId);
      else window.localStorage.removeItem(ACTIVE_PROJECT_KEY);
    }
  }, []);

  useEffect(() => {
    if (!projectsQuery.isSuccess || !activeProjectId) return;
    if (!projects.some((project) => project.id === activeProjectId)) setActive("");
  }, [projects, projectsQuery.isSuccess, activeProjectId, setActive]);

  const createActivity = useMutationActivity(mutationKeys.projectList.create);

  const renamingProjectIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.projectList.rename,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as { projectId?: string } | undefined)?.projectId ?? "",
  });
  const removingProjectIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.projectList.remove,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as string | undefined) ?? "",
  });
  const uploadingProjectIds = useMutationState<string>({
    filters: {
      mutationKey: mutationKeys.projectList.upload,
      exact: true,
      predicate: (mutation) => isMutationActive(mutation.state),
    },
    select: (mutation) => (mutation.state.variables as ProjectUploadVariables | undefined)?.projectId ?? "",
  });
  const renamingProjectIdSet = useMemo(() => new Set(renamingProjectIds), [renamingProjectIds]);
  const removingProjectIdSet = useMemo(() => new Set(removingProjectIds), [removingProjectIds]);
  const uploadingProjectIdSet = useMemo(() => new Set(uploadingProjectIds), [uploadingProjectIds]);
  const isRenamingProject = useCallback(
    (projectId: string) => renamingProjectIdSet.has(projectId),
    [renamingProjectIdSet],
  );
  const isRemovingProject = useCallback(
    (projectId: string) => removingProjectIdSet.has(projectId),
    [removingProjectIdSet],
  );
  const isUploadingProject = useCallback(
    (projectId: string) => uploadingProjectIdSet.has(projectId),
    [uploadingProjectIdSet],
  );

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const recover = useCallback(async () => {
    clearCoordinationError();
    removeFailedMutations(queryClient, PROJECT_LIST_MUTATION_KEYS);
    await queryClient.refetchQueries({ queryKey: PROJECTS_QUERY_KEY, type: "active" });
  }, [clearCoordinationError, queryClient]);

  const create = useCallback(
    async (name: string) => {
      const normalizedName = name.trim();
      const entityKey = "project-list:create";
      const operation = "create";
      const result = await runEntityAction(entityKey, operation, normalizedName, async () => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.projectList.create,
          meta: projectMutationMeta(entityKey, operation, normalizedName),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: PROJECTS_QUERY_KEY });
          },
          mutationFn: (desiredName: string) => createProject(desiredName),
          onSuccess: (project) => {
            queryClient.setQueryData<Project[]>(PROJECTS_QUERY_KEY, (current) => [...(current ?? []), project]);
            setActive(project.id);
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute(normalizedName);
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runEntityAction, setActive],
  );
  const remove = useCallback(
    async (projectId: string) => {
      const entityKey = `project:${projectId}`;
      const operation = "remove";
      const intentKey = projectId;
      const result = await runEntityAction(entityKey, operation, intentKey, async () => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.projectList.remove,
          meta: projectMutationMeta(entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: PROJECTS_QUERY_KEY });
          },
          mutationFn: (id: string) => deleteProject(id),
          onSuccess: (_result, id) => {
            queryClient.setQueryData<Project[]>(PROJECTS_QUERY_KEY, (current) =>
              (current ?? []).filter((project) => project.id !== id),
            );
            if (activeProjectId === id) setActive("");
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute(projectId);
      });
      resolveAction(result, entityKey, operation);
    },
    [activeProjectId, invalidate, queryClient, resolveAction, runEntityAction, setActive],
  );
  const rename = useCallback(
    async (projectId: string, name: string) => {
      const entityKey = `project:${projectId}`;
      const operation = "rename";
      const intentKey = name.trim();
      const result = await runEntityAction(entityKey, operation, intentKey, async () => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.projectList.rename,
          meta: projectMutationMeta(entityKey, operation, intentKey),
          onMutate: async () => {
            await queryClient.cancelQueries({ queryKey: PROJECTS_QUERY_KEY });
          },
          mutationFn: ({ projectId: id, name: n }: { projectId: string; name: string }) => renameProject(id, n.trim()),
          onSuccess: (updated) => {
            queryClient.setQueryData<Project[]>(PROJECTS_QUERY_KEY, (current) =>
              (current ?? []).map((project) => (project.id === updated.id ? { ...project, name: updated.name } : project)),
            );
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute({ projectId, name: intentKey });
      });
      resolveAction(result, entityKey, operation);
    },
    [invalidate, queryClient, resolveAction, runEntityAction],
  );
  const uploadDocuments = useCallback(
    async (fileInput: Iterable<File>) => {
      const files = Array.from(fileInput);
      const projectId = activeProjectId;
      if (!files.length || !projectId) return;
      const entityKey = `project:${projectId}`;
      const operation = "upload";
      const intentKey = uploadIntent(files);
      const variables = {
        projectId,
        files,
        apiKey: settings.apiKey.trim() || undefined,
      };
      const result = await runEntityAction(entityKey, operation, intentKey, async () => {
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.projectList.upload,
          meta: projectMutationMeta(entityKey, operation, intentKey),
          mutationFn: ({ projectId: id, files: selectedFiles, apiKey }: ProjectUploadVariables) =>
            uploadProjectFiles(id, selectedFiles, { ocrEnabled: true, apiKey }),
          onSettled: () => void invalidate(),
        });
        return mutation.execute(variables);
      });
      resolveAction(result, entityKey, operation);
    },
    [activeProjectId, invalidate, queryClient, resolveAction, runEntityAction, settings.apiKey],
  );

  const activeProject = useMemo(
    () => projects.find((project) => project.id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );

  const chatContext = useCallback((): ProjectChatContext => {
    if (!activeProject) return { projectAttachments: [] };
    return {
      projectId: activeProject.id,
      projectAttachments: projectDocumentsToAttachments(activeProject),
      memoryScope: `project:${activeProject.id}`,
    };
  }, [activeProject]);

  const mutationErrors = useMutationState<MutationStateSnapshot>({
    filters: { predicate: (mutation) => ownsMutationKey(mutation.options.mutationKey, PROJECT_LIST_MUTATION_KEYS) },
    select: (mutation) => ({
      status: mutation.state.status,
      error: mutation.state.error,
      submittedAt: mutation.state.submittedAt,
    }),
  });

  const firstError =
    projectsQuery.error
    ?? latestCacheMutationError(mutationErrors);

  return {
    projects,
    activeProjectId,
    activeProject,
    loading: projectsQuery.isLoading,
    refreshing: projectsQuery.isFetching && !projectsQuery.isLoading,
    uploading: isUploadingProject(activeProjectId),
    creating: createActivity.active,
    error: coordinationError || (firstError ? errorText(firstError, "项目操作失败") : ""),
    refresh,
    recover,
    create,
    remove,
    rename,
    isRenamingProject,
    isRemovingProject,
    isUploadingProject,
    setActive,
    uploadDocuments,
    chatContext,
  };
}
