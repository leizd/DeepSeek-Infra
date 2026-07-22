// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Skill } from "../../api/skillsApi";

const createMock = vi.fn<() => Promise<void>>();
const removeMock = vi.fn<() => Promise<void>>();

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
    error: "",
    create: createMock,
    remove: removeMock,
    update: vi.fn(() => Promise.resolve()),
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
});
