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
  it("does not trigger any manual refresh while the drawer is open", async () => {
    overlayState = { activeOverlay: "memory" };
    const view = render(<MemoryDrawer />);
    await waitFor(() => expect(refreshMock).not.toHaveBeenCalled());

    view.rerender(<MemoryDrawer />);
    view.rerender(<MemoryDrawer />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("does not refresh when the drawer closes and reopens (Query owns refetch)", async () => {
    overlayState = { activeOverlay: "memory" };
    const view = render(<MemoryDrawer />);
    overlayState = { activeOverlay: null };
    view.rerender(<MemoryDrawer />);
    overlayState = { activeOverlay: "memory" };
    view.rerender(<MemoryDrawer />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(refreshMock).not.toHaveBeenCalled();
  });
});
