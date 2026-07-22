// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";

import type { Project } from "../../api/projectsApi";
import { PROJECTS_QUERY_KEY } from "../../app/queryKeys";

vi.mock("../../api/projectsApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api/projectsApi")>();
  return {
    ...original,
    listProjects: vi.fn(),
    createProject: vi.fn(),
    deleteProject: vi.fn(),
    renameProject: vi.fn(),
    uploadProjectFiles: vi.fn(),
  };
});

vi.mock("../../contexts/SettingsContext", () => ({
  useSettings: () => ({ apiKey: "sk-test" }),
}));

import { createProject, deleteProject, listProjects, renameProject, uploadProjectFiles } from "../../api/projectsApi";
import { useProjectController } from "./useProjectController";

const listProjectsMock = vi.mocked(listProjects);
const createProjectMock = vi.mocked(createProject);
const deleteProjectMock = vi.mocked(deleteProject);
const renameProjectMock = vi.mocked(renameProject);
const uploadProjectFilesMock = vi.mocked(uploadProjectFiles);

function project(id: string, name: string): Project {
  return { id, name, documents: [], createdAt: 1, updatedAt: 1 };
}

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

let serverProjects: Project[];

beforeEach(() => {
  window.localStorage.clear();
  serverProjects = [project("p1", "调研"), project("p2", "代码")];
  listProjectsMock.mockImplementation(() => Promise.resolve([...serverProjects]));
  createProjectMock.mockImplementation((name: string) => {
    const created = project("p-new", name);
    serverProjects.push(created);
    return Promise.resolve(created);
  });
  deleteProjectMock.mockImplementation((projectId: string) => {
    serverProjects = serverProjects.filter((item) => item.id !== projectId);
    return Promise.resolve(undefined);
  });
  renameProjectMock.mockImplementation((projectId: string, name: string) => {
    serverProjects = serverProjects.map((item) => (item.id === projectId ? { ...item, name } : item));
    return Promise.resolve(project(projectId, name));
  });
  uploadProjectFilesMock.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useProjectController", () => {
  it("runs the initial query with an AbortSignal and exposes projects", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe("");

    const [init] = listProjectsMock.mock.calls[0] as [RequestInit];
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("activates the new project after creation and updates the cache", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    await act(async () => {
      await result.current.create("新项目");
    });

    expect(createProjectMock).toHaveBeenCalledWith("新项目");
    expect(result.current.activeProjectId).toBe("p-new");
    expect(window.localStorage.getItem("deepseek-infra.active-project")).toBe("p-new");
    expect(client.getQueryData<Project[]>(PROJECTS_QUERY_KEY)?.map((item) => item.id)).toContain("p-new");
  });

  it("updates the cache after rename and filters it after delete", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    await act(async () => {
      await result.current.rename("p1", "调研 v2");
    });
    expect(client.getQueryData<Project[]>(PROJECTS_QUERY_KEY)?.find((item) => item.id === "p1")?.name).toBe("调研 v2");

    await act(async () => {
      await result.current.remove("p2");
    });
    expect(client.getQueryData<Project[]>(PROJECTS_QUERY_KEY)?.map((item) => item.id)).toEqual(["p1"]);
  });

  it("refreshes the list after document upload", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    await act(async () => {
      await result.current.setActive("p1");
    });
    await act(async () => {
      await result.current.uploadDocuments([new File(["x"], "a.txt")]);
    });

    expect(uploadProjectFilesMock).toHaveBeenCalledWith("p1", [expect.any(File)], { ocrEnabled: true, apiKey: "sk-test" });
    await waitFor(() => expect(listProjectsMock.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("clears activeProjectId when the active project is deleted", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    await act(async () => {
      await result.current.setActive("p1");
    });
    expect(result.current.activeProject?.id).toBe("p1");

    await act(async () => {
      await result.current.remove("p1");
    });
    expect(result.current.activeProjectId).toBe("");
    expect(window.localStorage.getItem("deepseek-infra.active-project")).toBeNull();
    expect(result.current.activeProject).toBeNull();
  });

  it("repairs a stale activeProjectId restored from localStorage", async () => {
    window.localStorage.setItem("deepseek-infra.active-project", "deleted-project");
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });

    expect(result.current.activeProjectId).toBe("deleted-project");
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    await waitFor(() => expect(result.current.activeProjectId).toBe(""));
    expect(window.localStorage.getItem("deepseek-infra.active-project")).toBeNull();
  });

  it("clears a stale mutation error after a newer successful action and recover", async () => {
    createProjectMock.mockRejectedValueOnce(new Error("创建失败")).mockRejectedValueOnce(new Error("又失败"));
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    await act(async () => {
      await result.current.create("失败项目").catch(() => undefined);
    });
    await waitFor(() => expect(result.current.error).toBe("创建失败"));

    await act(async () => {
      await result.current.rename("p1", "新名字");
    });
    await waitFor(() => expect(result.current.error).toBe(""));

    await act(async () => {
      await result.current.create("又失败").catch(() => undefined);
    });
    await waitFor(() => expect(result.current.error).toBeTruthy());

    await act(async () => {
      await result.current.recover();
    });
    await waitFor(() => expect(result.current.error).toBe(""));
  });

  it("tracks concurrent removals independently", async () => {
    const resolvers = new Map<string, () => void>();
    deleteProjectMock.mockImplementation(
      (projectId: string) =>
        new Promise<void>((resolve) => {
          resolvers.set(projectId, () => {
            serverProjects = serverProjects.filter((item) => item.id !== projectId);
            resolve();
          });
        }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.remove("p1");
      second = result.current.remove("p2");
    });

    await waitFor(() => {
      expect(result.current.isRemovingProject("p1")).toBe(true);
      expect(result.current.isRemovingProject("p2")).toBe(true);
    });

    await act(async () => {
      resolvers.get("p1")?.();
      await first;
    });
    await waitFor(() => expect(result.current.isRemovingProject("p1")).toBe(false));
    expect(result.current.isRemovingProject("p2")).toBe(true);

    await act(async () => {
      resolvers.get("p2")?.();
      await second;
    });
    await waitFor(() => expect(result.current.isRemovingProject("p2")).toBe(false));
  });

  it("suppresses duplicate removal of the same project synchronously", async () => {
    let resolveDelete!: () => void;
    deleteProjectMock.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveDelete = resolve;
        }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let first!: Promise<void>;
    let duplicate!: Promise<void>;
    act(() => {
      first = result.current.remove("p1");
      duplicate = result.current.remove("p1");
    });
    await waitFor(() => expect(deleteProjectMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveDelete();
      await Promise.all([first, duplicate]);
    });
  });

  it("shares the original failure with a duplicate rename call", async () => {
    let rejectRename!: (reason: Error) => void;
    renameProjectMock.mockImplementation(
      () =>
        new Promise<Project>((_resolve, reject) => {
          rejectRename = reject;
        }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let first!: Promise<void>;
    let duplicate!: Promise<void>;
    act(() => {
      first = result.current.rename("p1", "新名字");
      duplicate = result.current.rename("p1", "新名字");
    });
    await waitFor(() => expect(renameProjectMock).toHaveBeenCalledTimes(1));

    const failure = new Error("rename failed");
    rejectRename(failure);
    await expect(first).rejects.toBe(failure);
    await expect(duplicate).rejects.toBe(failure);
  });

  it("cancels a stale list read before applying a removal", async () => {
    const staleSnapshot = [...serverProjects];
    let resolveStaleRead!: (projects: Project[]) => void;
    listProjectsMock
      .mockImplementationOnce(() => Promise.resolve([...serverProjects]))
      .mockImplementationOnce(
        () =>
          new Promise<Project[]>((resolve) => {
            resolveStaleRead = resolve;
          }),
      )
      .mockImplementation(() => Promise.resolve([...serverProjects]));
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let refreshPromise!: Promise<void>;
    act(() => {
      refreshPromise = result.current.refresh();
    });
    await waitFor(() => expect(listProjectsMock).toHaveBeenCalledTimes(2));

    await act(async () => {
      await result.current.remove("p1");
      await refreshPromise;
    });
    resolveStaleRead(staleSnapshot);

    await waitFor(() => {
      expect(client.getQueryData<Project[]>(PROJECTS_QUERY_KEY)?.map((item) => item.id)).toEqual(["p2"]);
    });
  });
});
