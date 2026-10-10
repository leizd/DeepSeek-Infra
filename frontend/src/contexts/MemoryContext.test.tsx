// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MemoryWriteController } from "../features/memory/useMemoryWriteController";

vi.mock("../api/memoryApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api/memoryApi")>();
  return { ...original, addMemory: vi.fn(), listMemories: vi.fn() };
});

import { addMemory, listMemories } from "../api/memoryApi";
import { MemoryProvider, useMemory } from "./MemoryContext";

const addMemoryMock = vi.mocked(addMemory);
const listMemoriesMock = vi.mocked(listMemories);

let controller: MemoryWriteController | null = null;

function Consumer() {
  controller = useMemory();
  return null;
}

beforeEach(() => {
  controller = null;
  addMemoryMock.mockResolvedValue({
    id: "m1",
    content: "按需保存",
    category: "fact",
    scope: "global",
    pinned: false,
    createdAt: "",
    updatedAt: "",
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MemoryProvider demand loading", () => {
  it("does not request the memory list on cold start", async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryProvider><Consumer /></MemoryProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(controller).not.toBeNull());
    expect(listMemoriesMock).not.toHaveBeenCalled();
  });

  it("saves a suggestion before the list capability has loaded", async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryProvider><Consumer /></MemoryProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(controller).not.toBeNull());
    await act(async () => {
      await expect(controller!.save({ content: "按需保存" })).resolves.toEqual({ saved: true, conflicts: [] });
    });
    expect(addMemoryMock).toHaveBeenCalledTimes(1);
    expect(listMemoriesMock).not.toHaveBeenCalled();
  });
});
