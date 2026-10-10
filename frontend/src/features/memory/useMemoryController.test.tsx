// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";

import { MemoryConflictError, type MemoryEntry } from "../../api/memoryApi";
import { MEMORIES_QUERY_KEY } from "../../app/queryKeys";

vi.mock("../../api/memoryApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api/memoryApi")>();
  return {
    ...original,
    listMemories: vi.fn(),
    deleteMemory: vi.fn(),
    clearMemories: vi.fn(),
    addMemory: vi.fn(),
  };
});

import { addMemory, clearMemories, deleteMemory, listMemories } from "../../api/memoryApi";
import { useMemoryController, type MemorySaveResult } from "./useMemoryController";

const listMemoriesMock = vi.mocked(listMemories);
const deleteMemoryMock = vi.mocked(deleteMemory);
const clearMemoriesMock = vi.mocked(clearMemories);
const addMemoryMock = vi.mocked(addMemory);

function entry(id: string, content: string): MemoryEntry {
  return { id, content, category: "fact", scope: "global", pinned: false, createdAt: "", updatedAt: "" };
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

let serverMemories: MemoryEntry[];

beforeEach(() => {
  serverMemories = [entry("m1", "偏好简洁"), entry("m2", "项目背景")];
  listMemoriesMock.mockImplementation(() => Promise.resolve([...serverMemories]));
  deleteMemoryMock.mockImplementation((memoryId: string) => {
    serverMemories = serverMemories.filter((item) => item.id !== memoryId);
    return Promise.resolve(undefined);
  });
  clearMemoriesMock.mockImplementation(() => {
    serverMemories = [];
    return Promise.resolve(undefined);
  });
  addMemoryMock.mockImplementation((input) => {
    const replaced = new Set(input.replaceIds ?? []);
    const saved = entry("m3", input.content);
    serverMemories = [...serverMemories.filter((item) => item.id !== saved.id && !replaced.has(item.id)), saved];
    return Promise.resolve(saved);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useMemoryController", () => {
  it("removes a single memory from the cache", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    const [init] = listMemoriesMock.mock.calls[0] as [RequestInit];
    expect(init.signal).toBeInstanceOf(AbortSignal);

    await act(async () => {
      await result.current.remove("m1");
    });
    expect(deleteMemoryMock).toHaveBeenCalledWith("m1");
    expect(client.getQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY)?.map((item) => item.id)).toEqual(["m2"]);
  });

  it("clears all memories and empties the cache", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    await act(async () => {
      await result.current.clear();
    });
    expect(clearMemoriesMock).toHaveBeenCalled();
    expect(client.getQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY)).toEqual([]);
  });

  it("invalidates after a successful save", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    let saveResult: MemorySaveResult | undefined;
    await act(async () => {
      saveResult = await result.current.save({ content: "记住这个", category: "fact", scope: "global" });
    });
    expect(saveResult).toEqual({ saved: true, conflicts: [] });
    expect(client.getQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY)?.map((item) => item.id)).toContain("m3");
    await waitFor(() => expect(listMemoriesMock.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("replaces conflicting entries in the cache when replaceIds are provided", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    await act(async () => {
      await result.current.save({ content: "偏好简洁", replaceIds: ["m2"] });
    });
    await waitFor(() =>
      expect(client.getQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY)?.map((item) => item.id)).toEqual(["m1", "m3"]),
    );
  });

  it("returns 409 conflicts without touching the query cache", async () => {
    addMemoryMock.mockRejectedValue(
      new MemoryConflictError("冲突", [{ id: "old-1", content: "旧记忆", category: "fact", scope: "global", reason: "similar" }]),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    const before = client.getQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY);
    let saveResult: MemorySaveResult | undefined;
    await act(async () => {
      saveResult = await result.current.save({ content: "重复记忆" });
    });

    expect(saveResult?.saved).toBe(false);
    expect(saveResult?.conflicts).toHaveLength(1);
    expect(saveResult?.conflicts[0].id).toBe("old-1");
    expect(client.getQueryData<MemoryEntry[]>(MEMORIES_QUERY_KEY)).toBe(before);
    expect(listMemoriesMock).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBe("");
  });

  it("deduplicates semantically identical normalized memory saves", async () => {
    let resolveSave!: (value: MemoryEntry) => void;
    addMemoryMock.mockImplementation(
      () => new Promise<MemoryEntry>((resolve) => {
        resolveSave = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    let first!: Promise<MemorySaveResult>;
    let duplicate!: Promise<MemorySaveResult>;
    act(() => {
      first = result.current.save({ content: "  同一记忆  ", replaceIds: ["m2", "m1"] });
      duplicate = result.current.save({
        content: "同一记忆",
        category: "fact",
        scope: "global",
        replaceIds: ["m1", "m2"],
      });
    });
    await waitFor(() => expect(addMemoryMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveSave(entry("m3", "同一记忆"));
      await expect(Promise.all([first, duplicate])).resolves.toEqual([
        { saved: true, conflicts: [] },
        { saved: true, conflicts: [] },
      ]);
    });
  });

  it("tracks concurrent memory removals independently", async () => {
    const resolvers = new Map<string, () => void>();
    deleteMemoryMock.mockImplementation(
      (memoryId: string) =>
        new Promise<void>((resolve) => {
          resolvers.set(memoryId, () => {
            serverMemories = serverMemories.filter((item) => item.id !== memoryId);
            resolve();
          });
        }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.remove("m1");
      second = result.current.remove("m2");
    });
    await waitFor(() => {
      expect(result.current.isRemovingMemory("m1")).toBe(true);
      expect(result.current.isRemovingMemory("m2")).toBe(true);
    });

    await act(async () => {
      resolvers.get("m1")?.();
      await first;
    });
    await waitFor(() => expect(result.current.isRemovingMemory("m1")).toBe(false));
    expect(result.current.isRemovingMemory("m2")).toBe(true);

    await act(async () => {
      resolvers.get("m2")?.();
      await second;
    });
  });

  it("does not run clear while a memory removal is pending", async () => {
    let resolveDelete!: () => void;
    deleteMemoryMock.mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    let removal!: Promise<void>;
    let clearAction!: Promise<void>;
    act(() => {
      removal = result.current.remove("m1");
      clearAction = result.current.clear();
    });

    await expect(clearAction).rejects.toMatchObject({
      name: "EntityActionConflictError",
      requestedEntityKey: "memory-list:clear",
      blocker: {
        entityKey: "memory:m1",
        operation: "remove",
      },
    });
    expect(clearMemoriesMock).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.hasPendingWrites).toBe(true));

    await act(async () => {
      resolveDelete();
      await removal;
    });
  });

  it("does not run clear while a memory save is pending", async () => {
    let resolveSave!: (value: MemoryEntry) => void;
    addMemoryMock.mockImplementation(
      () => new Promise<MemoryEntry>((resolve) => {
        resolveSave = resolve;
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    let saveAction!: Promise<MemorySaveResult>;
    let clearAction!: Promise<void>;
    act(() => {
      saveAction = result.current.save({ content: "新记忆" });
      clearAction = result.current.clear();
    });

    await expect(clearAction).rejects.toMatchObject({
      name: "EntityActionConflictError",
      blocker: {
        entityKey: expect.stringMatching(/^memory-save:/),
        operation: "save",
      },
    });
    expect(clearMemoriesMock).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(result.current.saving).toBe(true);
      expect(result.current.hasPendingWrites).toBe(true);
    });

    await act(async () => {
      resolveSave(entry("m3", "新记忆"));
      await saveAction;
    });
  });

  it("blocks remove and save while clear is pending", async () => {
    let resolveClear!: () => void;
    clearMemoriesMock.mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveClear = () => {
          serverMemories = [];
          resolve();
        };
      }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.memories).toHaveLength(2));

    let clearAction!: Promise<void>;
    let removeAction!: Promise<void>;
    let saveAction!: Promise<MemorySaveResult>;
    act(() => {
      clearAction = result.current.clear();
      removeAction = result.current.remove("m1");
      saveAction = result.current.save({ content: "不能并发" });
    });

    await expect(removeAction).rejects.toMatchObject({
      name: "EntityActionConflictError",
      requestedEntityKey: "memory:m1",
      blocker: { entityKey: "memory-list:clear", operation: "clear" },
    });
    await expect(saveAction).rejects.toMatchObject({
      name: "EntityActionConflictError",
      blocker: { entityKey: "memory-list:clear", operation: "clear" },
    });
    expect(deleteMemoryMock).not.toHaveBeenCalled();
    expect(addMemoryMock).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.clearing).toBe(true));

    await act(async () => {
      resolveClear();
      await clearAction;
    });
  });

  it("restores clearing state after remount and blocks a second clear", async () => {
    let resolveClear!: () => void;
    clearMemoriesMock.mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveClear = resolve;
      }),
    );
    const client = createTestQueryClient();
    const firstHook = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(firstHook.result.current.memories).toHaveLength(2));

    let clearAction!: Promise<void>;
    act(() => {
      clearAction = firstHook.result.current.clear();
    });
    await waitFor(() => expect(firstHook.result.current.clearing).toBe(true));
    firstHook.unmount();

    const remounted = renderHook(() => useMemoryController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(remounted.result.current.clearing).toBe(true));
    await expect(remounted.result.current.clear()).rejects.toMatchObject({
      name: "EntityActionConflictError",
      activeOperation: "clear",
    });
    expect(clearMemoriesMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveClear();
      await clearAction;
    });
  });
});
