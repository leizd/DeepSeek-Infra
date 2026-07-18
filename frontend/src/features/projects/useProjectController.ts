import { useCallback, useEffect, useMemo, useState } from "react";

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

export function useProjectController(): ProjectController {
  const settings = useSettings();
  const [projects, setProjects] = useState<readonly Project[]>([]);
  const [activeProjectId, setActiveProjectId] = useState(storedActiveProject);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setProjects(await listProjects());
    } catch (reason) {
      setError(reason instanceof Error && reason.message ? reason.message : "项目列表加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setActive = useCallback((projectId: string) => {
    setActiveProjectId(projectId);
    if (typeof window !== "undefined") {
      if (projectId) window.localStorage.setItem(ACTIVE_PROJECT_KEY, projectId);
      else window.localStorage.removeItem(ACTIVE_PROJECT_KEY);
    }
  }, []);

  const create = useCallback(
    async (name: string) => {
      const project = await createProject(name.trim());
      setProjects((current) => [...current, project]);
      setActive(project.id);
    },
    [setActive],
  );

  const remove = useCallback(
    async (projectId: string) => {
      await deleteProject(projectId);
      setProjects((current) => current.filter((project) => project.id !== projectId));
      if (activeProjectId === projectId) setActive("");
    },
    [activeProjectId, setActive],
  );

  const rename = useCallback(async (projectId: string, name: string) => {
    const project = await renameProject(projectId, name.trim());
    setProjects((current) => current.map((item) => (item.id === projectId ? { ...item, name: project.name } : item)));
  }, []);

  const uploadDocuments = useCallback(
    async (fileInput: Iterable<File>) => {
      const files = Array.from(fileInput);
      if (!files.length || !activeProjectId) return;
      setUploading(true);
      setError("");
      try {
        await uploadProjectFiles(activeProjectId, files, {
          ocrEnabled: true,
          apiKey: settings.apiKey.trim() || undefined,
        });
        await refresh();
      } catch (reason) {
        setError(reason instanceof Error && reason.message ? reason.message : "项目文档上传失败");
      } finally {
        setUploading(false);
      }
    },
    [activeProjectId, refresh, settings.apiKey],
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
    loading,
    uploading,
    error,
    refresh,
    create,
    remove,
    rename,
    setActive,
    uploadDocuments,
    chatContext,
  };
}
