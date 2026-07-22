// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Project } from "../../api/projectsApi";

const createMock = vi.fn<() => Promise<void>>();
const renameMock = vi.fn<() => Promise<void>>();
const removeMock = vi.fn<() => Promise<void>>();

const testProject: Project = {
  id: "p1",
  name: "原项目",
  documents: [],
  createdAt: 1,
  updatedAt: 1,
};

vi.mock("../../contexts/OverlayContext", () => ({
  useOverlay: () => ({ activeOverlay: "projects", closeOverlay: vi.fn() }),
}));

vi.mock("../../contexts/FilePreviewContext", () => ({
  useFilePreview: () => ({ open: vi.fn() }),
}));

vi.mock("../../contexts/SkillsContext", () => ({
  useSkills: () => ({ skills: [] }),
}));

vi.mock("../../contexts/ProjectsContext", () => ({
  useProjects: () => ({
    projects: [testProject],
    activeProjectId: "",
    activeProject: null,
    loading: false,
    refreshing: false,
    uploading: false,
    creating: false,
    error: "",
    create: createMock,
    rename: renameMock,
    remove: removeMock,
    recover: vi.fn(() => Promise.resolve()),
    setActive: vi.fn(),
    uploadDocuments: vi.fn(() => Promise.resolve()),
    isRenamingProject: vi.fn(() => false),
    isRemovingProject: vi.fn(() => false),
    isUploadingProject: vi.fn(() => false),
  }),
}));

vi.mock("./useProjectSkillBinding", () => ({
  useProjectSkillBinding: vi.fn(),
}));

import { ProjectsDrawer } from "./ProjectsDrawer";

beforeEach(() => {
  createMock.mockResolvedValue(undefined);
  renameMock.mockResolvedValue(undefined);
  removeMock.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("ProjectsDrawer mutation dispatch", () => {
  it("contains a rejected create action and preserves its draft", async () => {
    createMock.mockRejectedValueOnce(new Error("创建失败"));
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);
    render(<ProjectsDrawer />);

    const input = screen.getByPlaceholderText("新项目名称");
    fireEvent.change(input, { target: { value: "失败草稿" } });
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(createMock).toHaveBeenCalledWith("失败草稿"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(input).toHaveProperty("value", "失败草稿");
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });

  it("keeps the rename editor and draft open after failure", async () => {
    renameMock.mockRejectedValueOnce(new Error("重命名失败"));
    render(<ProjectsDrawer />);

    fireEvent.click(screen.getByRole("button", { name: "重命名项目 原项目" }));
    const input = screen.getByRole("textbox", { name: "重命名项目" });
    fireEvent.change(input, { target: { value: "修正后的名称" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(renameMock).toHaveBeenCalledWith("p1", "修正后的名称"));
    expect(screen.getByRole("textbox", { name: "重命名项目" })).toHaveProperty("value", "修正后的名称");
  });

  it("requires confirmation before deleting a project", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<ProjectsDrawer />);
    const button = screen.getByRole("button", { name: "删除项目 原项目" });

    fireEvent.click(button);
    expect(removeMock).not.toHaveBeenCalled();
    fireEvent.click(button);

    expect(confirm).toHaveBeenCalledWith("确定删除项目“原项目”？");
    expect(removeMock).toHaveBeenCalledWith("p1");
  });
});
