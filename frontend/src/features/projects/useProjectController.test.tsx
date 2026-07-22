// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";

import type { Project } from "../../api/projectsApi";
import { mutationKeys } from "../../app/mutationKeys";
import type { LifecycleMutationMeta } from "../../app/mutationLifecycle";
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

  it("does not replace a project selected while creation is pending", async () => {
    let resolveCreate!: (value: Project) => void;
    createProjectMock.mockImplementation(
      (name) => new Promise<Project>((resolve) => {
        resolveCreate = (value) => {
          serverProjects.push(project(value.id, name));
          resolve(value);
        };
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let creation!: Promise<void>;
    act(() => {
      creation = result.current.create("项目 C");
    });
    await waitFor(() => expect(createProjectMock).toHaveBeenCalledTimes(1));
    act(() => result.current.setActive("p2"));

    await act(async () => {
      resolveCreate(project("p3", "项目 C"));
      await creation;
    });
    expect(result.current.activeProjectId).toBe("p2");
    expect(window.localStorage.getItem("deepseek-infra.active-project")).toBe("p2");
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

  it("does not clear a newer selection when deletion of the old active project completes", async () => {
    let resolveDelete!: () => void;
    deleteProjectMock.mockImplementation(
      (projectId) => new Promise<void>((resolve) => {
        resolveDelete = () => {
          serverProjects = serverProjects.filter((item) => item.id !== projectId);
          resolve();
        };
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    act(() => result.current.setActive("p1"));

    let removal!: Promise<void>;
    act(() => {
      removal = result.current.remove("p1");
    });
    await waitFor(() => expect(deleteProjectMock).toHaveBeenCalledWith("p1"));
    act(() => result.current.setActive("p2"));

    await act(async () => {
      resolveDelete();
      await removal;
    });
    expect(result.current.activeProjectId).toBe("p2");
    expect(window.localStorage.getItem("deepseek-infra.active-project")).toBe("p2");
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

  it("clears a stale mutation error after a newer success on the same entity and recover", async () => {
    createProjectMock
      .mockRejectedValueOnce(new Error("创建失败"))
      .mockImplementationOnce(async (name) => {
        const created = project("p-success", name);
        serverProjects.push(created);
        return created;
      })
      .mockRejectedValueOnce(new Error("又失败"));
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    await act(async () => {
      await result.current.create("失败项目").catch(() => undefined);
    });
    await waitFor(() => expect(result.current.error).toBe("创建失败"));

    await act(async () => {
      await result.current.create("成功项目");
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

  it("keeps a late project failure visible after another project succeeds", async () => {
    let rejectDelete!: (reason: Error) => void;
    deleteProjectMock.mockImplementation(
      () => new Promise<void>((_resolve, reject) => {
        rejectDelete = reject;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let removal!: Promise<void>;
    act(() => {
      removal = result.current.remove("p1");
    });
    await waitFor(() => expect(deleteProjectMock).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.rename("p2", "代码 v2");
    });
    act(() => rejectDelete(new Error("删除 A 失败")));
    await act(async () => {
      await removal.catch(() => undefined);
    });

    await waitFor(() => expect(result.current.error).toBe("删除 A 失败"));
  });

  it("clears a coordination conflict when the blocking mutation settles", async () => {
    let resolveRename!: (value: Project) => void;
    renameProjectMock.mockImplementation(
      () => new Promise<Project>((resolve) => {
        resolveRename = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let renameAction!: Promise<void>;
    act(() => {
      renameAction = result.current.rename("p1", "等待名称");
    });
    await waitFor(() => expect(renameProjectMock).toHaveBeenCalledTimes(1));
    await expect(result.current.remove("p1")).rejects.toMatchObject({ name: "EntityActionConflictError" });
    await waitFor(() => expect(result.current.error).toContain("正在重命名"));

    await act(async () => {
      resolveRename(project("p1", "等待名称"));
      await renameAction;
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

  it("suppresses a same-frame duplicate project creation", async () => {
    let resolveCreate!: (value: Project) => void;
    createProjectMock.mockImplementation(
      () => new Promise<Project>((resolve) => {
        resolveCreate = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let first!: Promise<void>;
    let duplicate!: Promise<void>;
    act(() => {
      first = result.current.create("新项目");
      duplicate = result.current.create(" 新项目 ");
    });
    await waitFor(() => expect(createProjectMock).toHaveBeenCalledTimes(1));
    expect(result.current.creating).toBe(true);

    await act(async () => {
      resolveCreate(project("p-new", "新项目"));
      await Promise.all([first, duplicate]);
    });
  });

  it("rejects a different rename intent visibly and recover clears the coordination error", async () => {
    let resolveRename!: (value: Project) => void;
    renameProjectMock.mockImplementation(
      () => new Promise<Project>((resolve) => {
        resolveRename = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let first!: Promise<void>;
    let different!: Promise<void>;
    act(() => {
      first = result.current.rename("p1", "项目甲");
      different = result.current.rename("p1", "项目乙");
    });
    await expect(different).rejects.toMatchObject({
      name: "EntityActionConflictError",
      activeOperation: "rename",
    });
    expect(renameProjectMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.error).toContain("正在重命名"));

    await act(async () => {
      await result.current.recover();
    });
    expect(result.current.error).toBe("");

    await act(async () => {
      resolveRename(project("p1", "项目甲"));
      await first;
    });
  });

  it("does not reuse an upload promise for a different file batch", async () => {
    let resolveUpload!: (value: []) => void;
    uploadProjectFilesMock.mockImplementation(
      () => new Promise<[]>((resolve) => {
        resolveUpload = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    act(() => result.current.setActive("p1"));

    const firstFile = new File(["a"], "a.txt", { lastModified: 1 });
    const secondFile = new File(["b"], "b.txt", { lastModified: 2 });
    let first!: Promise<void>;
    let different!: Promise<void>;
    act(() => {
      first = result.current.uploadDocuments([firstFile]);
      different = result.current.uploadDocuments([secondFile]);
    });
    await expect(different).rejects.toMatchObject({ name: "EntityActionConflictError" });
    expect(uploadProjectFilesMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveUpload([]);
      await first;
    });
  });

  it("keeps binding failures local and preserves them during project recovery", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    const bindingMutation = client.getMutationCache().build(client, {
      mutationKey: mutationKeys.projectBinding.save("p1"),
      mutationFn: async (_value: unknown) => {
        throw new Error("绑定保存失败");
      },
    });
    await act(async () => {
      await bindingMutation.execute(undefined).catch(() => undefined);
    });

    expect(bindingMutation.state.status).toBe("error");
    expect(result.current.error).toBe("");

    await act(async () => {
      await result.current.recover();
    });
    expect(client.getMutationCache().findAll({
      mutationKey: mutationKeys.projectBinding.save("p1"),
      exact: true,
    })).toContain(bindingMutation);
  });

  it("preserves a pending removal and its pending UI state during recovery", async () => {
    let resolveDelete!: () => void;
    deleteProjectMock.mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let removal!: Promise<void>;
    act(() => {
      removal = result.current.remove("p1");
    });
    await waitFor(() => expect(result.current.isRemovingProject("p1")).toBe(true));

    await act(async () => {
      await result.current.recover();
    });
    expect(result.current.isRemovingProject("p1")).toBe(true);
    expect(client.getMutationCache().findAll({
      mutationKey: mutationKeys.projectList.remove,
      exact: true,
      status: "pending",
    })).toHaveLength(1);

    await act(async () => {
      resolveDelete();
      await removal;
    });
  });

  it("rejects a conflicting remove while the same project is being renamed", async () => {
    let resolveRename!: (value: Project) => void;
    renameProjectMock.mockImplementation(
      () => new Promise<Project>((resolve) => {
        resolveRename = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));

    let renameAction!: Promise<void>;
    let removeAction!: Promise<void>;
    act(() => {
      renameAction = result.current.rename("p1", "新名称");
      removeAction = result.current.remove("p1");
    });
    await expect(removeAction).rejects.toMatchObject({ name: "EntityActionConflictError" });
    await waitFor(() => expect(renameProjectMock).toHaveBeenCalledTimes(1));
    expect(deleteProjectMock).not.toHaveBeenCalled();

    await act(async () => {
      resolveRename(project("p1", "新名称"));
      await renameAction;
    });
  });

  it("keeps an upload bound to its original project after the active project changes", async () => {
    let resolveUpload!: (value: []) => void;
    uploadProjectFilesMock.mockImplementation(
      () => new Promise<[]>((resolve) => {
        resolveUpload = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    act(() => result.current.setActive("p1"));

    let upload!: Promise<void>;
    act(() => {
      upload = result.current.uploadDocuments([new File(["x"], "a.txt")]);
      result.current.setActive("p2");
    });

    await waitFor(() => expect(uploadProjectFilesMock).toHaveBeenCalledWith(
      "p1",
      [expect.any(File)],
      { ocrEnabled: true, apiKey: "sk-test" },
    ));
    expect(result.current.isUploadingProject("p1")).toBe(true);
    expect(result.current.isUploadingProject("p2")).toBe(false);
    expect(result.current.uploading).toBe(false);

    await act(async () => {
      resolveUpload([]);
      await upload;
    });
  });

  it("blocks deletion while the target project is uploading", async () => {
    let resolveUpload!: (value: []) => void;
    uploadProjectFilesMock.mockImplementation(
      () => new Promise<[]>((resolve) => {
        resolveUpload = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    act(() => result.current.setActive("p1"));

    let upload!: Promise<void>;
    let removal!: Promise<void>;
    act(() => {
      upload = result.current.uploadDocuments([new File(["x"], "a.txt")]);
      removal = result.current.remove("p1");
    });
    await expect(removal).rejects.toMatchObject({ name: "EntityActionConflictError" });
    expect(deleteProjectMock).not.toHaveBeenCalled();

    await act(async () => {
      resolveUpload([]);
      await upload;
    });
  });

  it("attributes project deletion conflicts to the matching binding save", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.projects).toHaveLength(2));
    let releaseBinding!: () => void;
    const meta: LifecycleMutationMeta = {
      owner: "project-binding",
      lifecycleId: "binding-save-p1",
      entityKey: "project-binding:p1",
      operation: "save",
      intentKey: "binding-a",
    };
    const mutation = client.getMutationCache().build(client, {
      mutationKey: mutationKeys.projectBinding.save("p1"),
      meta,
      mutationFn: () => new Promise<void>((resolve) => { releaseBinding = resolve; }),
    });
    const bindingAction = mutation.execute(undefined);
    await waitFor(() => expect(result.current.isProjectBindingSaving("p1")).toBe(true));

    await expect(result.current.remove("p1")).rejects.toMatchObject({
      name: "EntityActionConflictError",
      requestedEntityKey: "project:p1",
      blocker: {
        lifecycleId: "binding-save-p1",
        entityKey: "project-binding:p1",
        operation: "save",
      },
    });
    expect(deleteProjectMock).not.toHaveBeenCalled();

    await act(async () => {
      releaseBinding();
      await bindingAction;
    });
  });

  it("restores a pending upload after remount and does not send it again", async () => {
    let resolveUpload!: (value: []) => void;
    uploadProjectFilesMock.mockImplementation(
      () => new Promise<[]>((resolve) => {
        resolveUpload = resolve;
      }),
    );
    const client = createTestQueryClient();
    const firstHook = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(firstHook.result.current.projects).toHaveLength(2));
    act(() => firstHook.result.current.setActive("p1"));
    const file = new File(["a"], "a.txt", { lastModified: 1 });

    let upload!: Promise<void>;
    act(() => {
      upload = firstHook.result.current.uploadDocuments([file]);
    });
    await waitFor(() => expect(firstHook.result.current.isUploadingProject("p1")).toBe(true));
    firstHook.unmount();

    const remounted = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(remounted.result.current.isUploadingProject("p1")).toBe(true));
    await expect(remounted.result.current.uploadDocuments([file])).rejects.toMatchObject({
      name: "EntityActionConflictError",
      activeOperation: "upload",
    });
    expect(uploadProjectFilesMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveUpload([]);
      await upload;
    });
  });

  it("prevents a pending project removal from being submitted again after remount", async () => {
    let resolveDelete!: () => void;
    deleteProjectMock.mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );
    const client = createTestQueryClient();
    const firstHook = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(firstHook.result.current.projects).toHaveLength(2));

    let removal!: Promise<void>;
    act(() => {
      removal = firstHook.result.current.remove("p1");
    });
    await waitFor(() => expect(firstHook.result.current.isRemovingProject("p1")).toBe(true));
    firstHook.unmount();

    const remounted = renderHook(() => useProjectController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(remounted.result.current.isRemovingProject("p1")).toBe(true));
    await expect(remounted.result.current.remove("p1")).rejects.toMatchObject({
      name: "EntityActionConflictError",
      activeOperation: "remove",
    });
    expect(deleteProjectMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveDelete();
      await removal;
    });
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
