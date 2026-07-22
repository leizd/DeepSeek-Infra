import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createProject,
  deleteProject,
  listProjects,
  renameProject,
  uploadProjectFiles,
  type Project,
} from "../../api/projectsApi";
import type { Attachment } from "../../domain/chat/types";
import { mutationKeys } from "../../app/mutationKeys";
import { PROJECTS_QUERY_KEY } from "../../app/queryKeys";
import { latestCacheMutationError, type MutationStateSnapshot } from "../../app/mutationErrors";
import { useSettings } from "../../contexts/SettingsContext";
import { useActionLocks } from "../../shared/useActionLocks";

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
  setActive(projectId: string): void;
  uploadDocuments(files: Iterable<File>): Promise<void>;
  chatContext(): ProjectChatContext;
}

function storedActiveProject(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ACTIVE_PROJECT_KEY) ?? "";
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function useProjectController(): ProjectController {
  const settings = useSettings();
  const queryClient = useQueryClient();
  const runLocked = useActionLocks();
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

  const createMutation = useMutation({
    mutationKey: mutationKeys.projects.create,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: PROJECTS_QUERY_KEY });
    },
    mutationFn: (name: string) => createProject(name.trim()),
    onSuccess: (project) => {
      queryClient.setQueryData<Project[]>(PROJECTS_QUERY_KEY, (current) => [...(current ?? []), project]);
      setActive(project.id);
    },
    onSettled: () => void invalidate(),
  });

  const uploadMutation = useMutation({
    mutationKey: mutationKeys.projects.upload,
    mutationFn: (files: File[]) =>
      uploadProjectFiles(activeProjectId, files, {
        ocrEnabled: true,
        apiKey: settings.apiKey.trim() || undefined,
      }),
    onSettled: () => void invalidate(),
  });

  const renamingProjectIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.projects.rename, status: "pending" },
    select: (mutation) => (mutation.state.variables as { projectId?: string } | undefined)?.projectId ?? "",
  });
  const removingProjectIds = useMutationState<string>({
    filters: { mutationKey: mutationKeys.projects.remove, status: "pending" },
    select: (mutation) => (mutation.state.variables as string | undefined) ?? "",
  });
  const renamingProjectIdSet = useMemo(() => new Set(renamingProjectIds), [renamingProjectIds]);
  const removingProjectIdSet = useMemo(() => new Set(removingProjectIds), [removingProjectIds]);
  const isRenamingProject = useCallback(
    (projectId: string) => renamingProjectIdSet.has(projectId),
    [renamingProjectIdSet],
  );
  const isRemovingProject = useCallback(
    (projectId: string) => removingProjectIdSet.has(projectId),
    [removingProjectIdSet],
  );

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const recover = useCallback(async () => {
    const cache = queryClient.getMutationCache();
    for (const key of [mutationKeys.projects.create, mutationKeys.projects.rename, mutationKeys.projects.remove, mutationKeys.projects.upload]) {
      cache.findAll({ mutationKey: key }).forEach((m) => cache.remove(m));
    }
    await invalidate();
  }, [invalidate, queryClient]);

  const create = useCallback(
    async (name: string) => {
      await createMutation.mutateAsync(name);
    },
    [createMutation],
  );
  const remove = useCallback(
    async (projectId: string) => {
      await runLocked(`project:remove:${projectId}`, async () => {
        await queryClient.cancelQueries({ queryKey: PROJECTS_QUERY_KEY });
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.projects.remove,
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
    },
    [activeProjectId, invalidate, queryClient, runLocked, setActive],
  );
  const rename = useCallback(
    async (projectId: string, name: string) => {
      await runLocked(`project:rename:${projectId}`, async () => {
        await queryClient.cancelQueries({ queryKey: PROJECTS_QUERY_KEY });
        const mutation = queryClient.getMutationCache().build(queryClient, {
          mutationKey: mutationKeys.projects.rename,
          mutationFn: ({ projectId: id, name: n }: { projectId: string; name: string }) => renameProject(id, n.trim()),
          onSuccess: (updated) => {
            queryClient.setQueryData<Project[]>(PROJECTS_QUERY_KEY, (current) =>
              (current ?? []).map((project) => (project.id === updated.id ? { ...project, name: updated.name } : project)),
            );
          },
          onSettled: () => void invalidate(),
        });
        return mutation.execute({ projectId, name });
      });
    },
    [invalidate, queryClient, runLocked],
  );
  const uploadDocuments = useCallback(
    async (fileInput: Iterable<File>) => {
      const files = Array.from(fileInput);
      if (!files.length || !activeProjectId) return;
      await uploadMutation.mutateAsync(files);
    },
    [activeProjectId, uploadMutation],
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
    filters: { predicate: (mutation) => { const key = mutation.options.mutationKey; return Array.isArray(key) && key.length >= 2 && key[0] === "projects"; } },
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
    uploading: uploadMutation.isPending,
    creating: createMutation.isPending,
    error: firstError ? errorText(firstError, "项目操作失败") : "",
    refresh,
    recover,
    create,
    remove,
    rename,
    isRenamingProject,
    isRemovingProject,
    setActive,
    uploadDocuments,
    chatContext,
  };
}
