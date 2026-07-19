import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createProject,
  deleteProject,
  listProjects,
  renameProject,
  uploadProjectFiles,
  type Project,
} from "../../api/projectsApi";
import type { Attachment } from "../../domain/chat/types";
import { useSettings } from "../../contexts/SettingsContext";

const ACTIVE_PROJECT_KEY = "deepseek-infra.active-project";
export const PROJECTS_QUERY_KEY = ["projects"] as const;

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
  uploading: boolean;
  error: string;
  refresh(): Promise<void>;
  create(name: string): Promise<void>;
  remove(projectId: string): Promise<void>;
  rename(projectId: string, name: string): Promise<void>;
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
  const [activeProjectId, setActiveProjectId] = useState(storedActiveProject);
  const [actionError, setActionError] = useState("");

  const projectsQuery = useQuery<Project[]>({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: () => listProjects(),
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

  const createMutation = useMutation({
    mutationFn: (name: string) => createProject(name.trim()),
    onSuccess: (project) => {
      setActionError("");
      setActive(project.id);
      void invalidate();
    },
    onError: (reason) => setActionError(errorText(reason, "项目创建失败")),
  });

  const removeMutation = useMutation({
    mutationFn: (projectId: string) => deleteProject(projectId),
    onSuccess: (_result, projectId) => {
      setActionError("");
      if (activeProjectId === projectId) setActive("");
      void invalidate();
    },
    onError: (reason) => setActionError(errorText(reason, "项目删除失败")),
  });

  const renameMutation = useMutation({
    mutationFn: ({ projectId, name }: { projectId: string; name: string }) => renameProject(projectId, name.trim()),
    onSuccess: () => {
      setActionError("");
      void invalidate();
    },
    onError: (reason) => setActionError(errorText(reason, "项目重命名失败")),
  });

  const uploadMutation = useMutation({
    mutationFn: (files: File[]) =>
      uploadProjectFiles(activeProjectId, files, {
        ocrEnabled: true,
        apiKey: settings.apiKey.trim() || undefined,
      }),
    onSuccess: () => {
      setActionError("");
      void invalidate();
    },
    onError: (reason) => setActionError(errorText(reason, "项目文档上传失败")),
  });

  const refresh = useCallback(async () => {
    await invalidate();
  }, [invalidate]);

  const create = useCallback(
    async (name: string) => {
      await createMutation.mutateAsync(name);
    },
    [createMutation],
  );
  const remove = useCallback(
    async (projectId: string) => {
      await removeMutation.mutateAsync(projectId);
    },
    [removeMutation],
  );
  const rename = useCallback(
    async (projectId: string, name: string) => {
      await renameMutation.mutateAsync({ projectId, name });
    },
    [renameMutation],
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

  return {
    projects,
    activeProjectId,
    activeProject,
    loading: projectsQuery.isLoading,
    uploading: uploadMutation.isPending,
    error: actionError || (projectsQuery.error ? errorText(projectsQuery.error, "项目列表加载失败") : ""),
    refresh,
    create,
    remove,
    rename,
    setActive,
    uploadDocuments,
    chatContext,
  };
}
