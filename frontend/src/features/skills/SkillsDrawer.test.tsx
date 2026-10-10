// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Skill } from "../../api/skillsApi";

const createMock = vi.fn<() => Promise<void>>();
const removeMock = vi.fn<() => Promise<void>>();
const updateMock = vi.fn<() => Promise<void>>();

const testSkill: Skill = {
  skillId: "s1",
  name: "自定义技能",
  description: "说明",
  version: "1.0.0",
  systemPrompt: "原提示词",
  builtin: false,
  disabled: false,
  updatedAt: "",
};

vi.mock("../../contexts/OverlayContext", () => ({
  useOverlay: () => ({ activeOverlay: "skills", closeOverlay: vi.fn() }),
}));

vi.mock("../../contexts/SkillsContext", () => ({
  useSkills: () => ({
    skills: [testSkill],
    loading: false,
    refreshing: false,
    creating: false,
    error: "",
    create: createMock,
    remove: removeMock,
    update: updateMock,
    toggle: vi.fn(() => Promise.resolve()),
    recover: vi.fn(() => Promise.resolve()),
    isUpdatingSkill: vi.fn(() => false),
    isTogglingSkill: vi.fn(() => false),
    isRemovingSkill: vi.fn(() => false),
  }),
}));

import { SkillsDrawer } from "./SkillsDrawer";

beforeEach(() => {
  createMock.mockResolvedValue(undefined);
  removeMock.mockResolvedValue(undefined);
  updateMock.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("SkillsDrawer mutation dispatch", () => {
  it("restores form availability and preserves the draft after failure", async () => {
    let rejectCreate!: (reason: Error) => void;
    createMock.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectCreate = reject;
        }),
    );
    render(<SkillsDrawer />);

    fireEvent.click(screen.getByRole("button", { name: "新建技能" }));
    fireEvent.change(screen.getByRole("textbox", { name: "技能名称" }), { target: { value: "失败草稿" } });
    fireEvent.change(screen.getByRole("textbox", { name: "技能提示词" }), { target: { value: "保留提示词" } });
    const submit = screen.getByRole("button", { name: "创建技能" });
    fireEvent.click(submit);
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    rejectCreate(new Error("创建失败"));
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByRole("textbox", { name: "技能名称" })).toHaveProperty("value", "失败草稿");
    expect(screen.getByRole("textbox", { name: "技能提示词" })).toHaveProperty("value", "保留提示词");
  });

  it("requires confirmation before deleting a custom skill", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<SkillsDrawer />);
    const button = screen.getByRole("button", { name: "删除" });

    fireEvent.click(button);
    expect(removeMock).not.toHaveBeenCalled();
    fireEvent.click(button);

    expect(confirm).toHaveBeenCalledWith("确定删除技能“自定义技能”？");
    expect(removeMock).toHaveBeenCalledWith("s1");
  });

  it("does not close a newly reopened create form when an old submission completes", async () => {
    let resolveCreate!: () => void;
    createMock.mockImplementationOnce(() => new Promise<void>((resolve) => {
      resolveCreate = resolve;
    }));
    render(<SkillsDrawer />);

    fireEvent.click(screen.getByRole("button", { name: "新建技能" }));
    fireEvent.change(screen.getByRole("textbox", { name: "技能名称" }), { target: { value: "技能 A" } });
    fireEvent.change(screen.getByRole("textbox", { name: "技能提示词" }), { target: { value: "提示 A" } });
    fireEvent.click(screen.getByRole("button", { name: "创建技能" }));
    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "新建技能" }));
    fireEvent.change(screen.getByRole("textbox", { name: "技能名称" }), { target: { value: "技能 B" } });
    fireEvent.change(screen.getByRole("textbox", { name: "技能提示词" }), { target: { value: "提示 B" } });
    resolveCreate();

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "技能名称" })).toHaveProperty("value", "技能 B");
    });
  });

  it("keeps a draft edited after submission when the old save completes", async () => {
    let resolveUpdate!: () => void;
    updateMock.mockImplementationOnce(() => new Promise<void>((resolve) => {
      resolveUpdate = resolve;
    }));
    render(<SkillsDrawer />);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const nameInput = screen.getByRole("textbox", { name: "技能名称" });
    fireEvent.change(nameInput, { target: { value: "已提交名称" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    fireEvent.change(nameInput, { target: { value: "等待提交的新名称" } });
    resolveUpdate();

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "技能名称" })).toHaveProperty("value", "等待提交的新名称");
    });
  });
});
