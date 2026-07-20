// @vitest-environment jsdom

import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

let overlayState: { activeOverlay: string | null };
const refreshMock = vi.fn();

vi.mock("../../contexts/OverlayContext", () => ({
  useOverlay: () => ({
    get activeOverlay() {
      return overlayState.activeOverlay;
    },
    closeOverlay: vi.fn(),
    openOverlay: vi.fn(),
  }),
}));

vi.mock("../../contexts/MemoryContext", () => ({
  useMemory: () => ({
    memories: [],
    loading: false,
    refreshing: false,
    error: "",
    refresh: refreshMock,
    remove: vi.fn(),
    clear: vi.fn(),
    save: vi.fn(),
  }),
}));

import { MemoryDrawer } from "./MemoryDrawer";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MemoryDrawer refresh effect", () => {
  it("refreshes exactly once while the drawer stays open", async () => {
    overlayState = { activeOverlay: "memory" };
    const view = render(<MemoryDrawer />);
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1));

    view.rerender(<MemoryDrawer />);
    view.rerender(<MemoryDrawer />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(refreshMock).toHaveBeenCalledTimes(1);
  });

  it("refreshes again only after a close and reopen", async () => {
    overlayState = { activeOverlay: "memory" };
    const view = render(<MemoryDrawer />);
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1));

    overlayState = { activeOverlay: null };
    view.rerender(<MemoryDrawer />);
    expect(refreshMock).toHaveBeenCalledTimes(1);

    overlayState = { activeOverlay: "memory" };
    view.rerender(<MemoryDrawer />);
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(2));
  });
});
