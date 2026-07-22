// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";

import type { ProjectSkillBinding } from "../../api/skillsApi";
import { projectSkillBindingQueryKey } from "../../app/queryKeys";

vi.mock("../../api/skillsApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api/skillsApi")>();
  return {
    ...original,
    fetchProjectSkillBinding: vi.fn(),
    saveProjectSkillBinding: vi.fn(),
  };
});

import { fetchProjectSkillBinding, saveProjectSkillBinding } from "../../api/skillsApi";
import { useProjectSkillBinding } from "./useProjectSkillBinding";

const fetchBindingMock = vi.mocked(fetchProjectSkillBinding);
const saveBindingMock = vi.mocked(saveProjectSkillBinding);

function binding(enabledSkills: readonly string[], defaultSkill = ""): ProjectSkillBinding {
  return { enabledSkills: [...enabledSkills], defaultSkill, recentSkills: [], enabledPacks: [] };
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useProjectSkillBinding", () => {
  it("loads the binding declaratively and exposes loading state", async () => {
    fetchBindingMock.mockResolvedValue(binding(["s1"], "s1"));
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.binding?.enabledSkills).toEqual(["s1"]));
    expect(result.current.loading).toBe(false);
    const [projectId, init] = fetchBindingMock.mock.calls[0] as [string, RequestInit];
    expect(projectId).toBe("p1");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("writes the saved binding into the cache", async () => {
    let serverBinding = binding([]);
    fetchBindingMock.mockImplementation(() => Promise.resolve(serverBinding));
    saveBindingMock.mockImplementation((_projectId, input) => {
      serverBinding = binding(input.enabledSkills, input.defaultSkill);
      return Promise.resolve(serverBinding);
    });
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.save(binding(["s2"], "s2"));
    });

    expect(saveBindingMock).toHaveBeenCalledWith("p1", { enabledSkills: ["s2"], defaultSkill: "s2" });
    expect(client.getQueryData<ProjectSkillBinding>(projectSkillBindingQueryKey("p1"))).toMatchObject({
      enabledSkills: ["s2"],
      defaultSkill: "s2",
    });
  });

  it("rejects a different binding intent while the first save is pending", async () => {
    let serverBinding = binding([]);
    fetchBindingMock.mockImplementation(() => Promise.resolve(serverBinding));
    let resolveFirst!: (value: ProjectSkillBinding) => void;
    saveBindingMock.mockImplementation(
      () => new Promise<ProjectSkillBinding>((resolve) => {
        resolveFirst = (value) => {
          serverBinding = value;
          resolve(value);
        };
      }),
    );

    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.loading).toBe(false));

    let firstPromise!: Promise<ProjectSkillBinding>;
    let secondPromise!: Promise<ProjectSkillBinding>;
    act(() => {
      firstPromise = result.current.save(binding(["a"]));
      secondPromise = result.current.save(binding(["b"]));
    });

    await expect(secondPromise).rejects.toMatchObject({ name: "EntityActionConflictError" });
    await waitFor(() => expect(result.current.saving).toBe(true));
    expect(saveBindingMock).toHaveBeenCalledTimes(1);

    resolveFirst(binding(["a"]));
    await act(async () => {
      await firstPromise;
    });

    expect(client.getQueryData<ProjectSkillBinding>(projectSkillBindingQueryKey("p1"))?.enabledSkills).toEqual(["a"]);
  });

  it("restores binding saving state after remount without resubmitting", async () => {
    fetchBindingMock.mockResolvedValue(binding([]));
    let resolveSave!: (value: ProjectSkillBinding) => void;
    saveBindingMock.mockImplementation(
      () => new Promise<ProjectSkillBinding>((resolve) => {
        resolveSave = resolve;
      }),
    );
    const client = createTestQueryClient();
    const firstHook = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(firstHook.result.current.loading).toBe(false));
    const desired = binding(["s1"], "s1");

    let saveAction!: Promise<ProjectSkillBinding>;
    act(() => {
      saveAction = firstHook.result.current.save(desired);
    });
    await waitFor(() => expect(firstHook.result.current.saving).toBe(true));
    firstHook.unmount();

    const remounted = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(remounted.result.current.saving).toBe(true));
    await expect(remounted.result.current.save(desired)).rejects.toMatchObject({
      name: "EntityActionConflictError",
      activeOperation: "save",
    });
    expect(saveBindingMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSave(desired);
      await saveAction;
    });
  });

  it("surfaces fetch errors with a retry path", async () => {
    fetchBindingMock.mockRejectedValueOnce(new Error("network down")).mockResolvedValueOnce(binding(["s1"]));
    const client = createTestQueryClient();
    const { result } = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.errorKind).toBe("load");
    await act(async () => {
      await result.current.retry();
    });
    await waitFor(() => expect(result.current.binding?.enabledSkills).toEqual(["s1"]));
    expect(result.current.error).toBeNull();
    expect(result.current.errorKind).toBeNull();
  });

  it("restores a failed save after remount and retries the last desired binding", async () => {
    let serverBinding = binding([]);
    fetchBindingMock.mockImplementation(() => Promise.resolve(serverBinding));
    saveBindingMock
      .mockRejectedValueOnce(new Error("save failed"))
      .mockImplementation((_projectId, input) => {
        serverBinding = binding(input.enabledSkills, input.defaultSkill);
        return Promise.resolve(serverBinding);
      });
    const client = createTestQueryClient();
    const firstHook = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(firstHook.result.current.loading).toBe(false));

    await act(async () => {
      await firstHook.result.current.save(binding(["s1"], "s1")).catch(() => undefined);
    });
    await waitFor(() => expect(firstHook.result.current.error).toBeTruthy());
    firstHook.unmount();

    const remounted = renderHook(() => useProjectSkillBinding("p1"), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(remounted.result.current.error).toBeTruthy());
    expect(remounted.result.current.errorKind).toBe("save");

    await act(async () => {
      await remounted.result.current.retry();
    });
    await waitFor(() => expect(remounted.result.current.error).toBeNull());
    expect(saveBindingMock).toHaveBeenLastCalledWith("p1", { enabledSkills: ["s1"], defaultSkill: "s1" });
    expect(client.getQueryData<ProjectSkillBinding>(projectSkillBindingQueryKey("p1"))?.enabledSkills).toEqual(["s1"]);
  });
});
