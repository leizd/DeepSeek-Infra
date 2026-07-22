// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";

import type { ProjectSkillBinding, Skill } from "../../api/skillsApi";
import { SKILLS_QUERY_KEY, projectSkillBindingQueryKey } from "../../app/queryKeys";

vi.mock("../../api/skillsApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api/skillsApi")>();
  return {
    ...original,
    listSkills: vi.fn(),
    setSkillDisabled: vi.fn(),
    deleteSkill: vi.fn(),
    createSkill: vi.fn(),
    updateSkillPrompt: vi.fn(),
    fetchProjectSkillBinding: vi.fn(),
    saveProjectSkillBinding: vi.fn(),
  };
});

import {
  createSkill,
  deleteSkill,
  fetchProjectSkillBinding,
  listSkills,
  saveProjectSkillBinding,
  setSkillDisabled,
  updateSkillPrompt,
} from "../../api/skillsApi";
import { useSkillController } from "./useSkillController";

const listSkillsMock = vi.mocked(listSkills);
const setSkillDisabledMock = vi.mocked(setSkillDisabled);
const deleteSkillMock = vi.mocked(deleteSkill);
const createSkillMock = vi.mocked(createSkill);
const updateSkillPromptMock = vi.mocked(updateSkillPrompt);
const fetchBindingMock = vi.mocked(fetchProjectSkillBinding);
const saveBindingMock = vi.mocked(saveProjectSkillBinding);

function skill(skillId: string, disabled = false): Skill {
  return { skillId, name: skillId, description: "", version: "1.0.0", systemPrompt: "", builtin: false, disabled, updatedAt: "" };
}

function binding(enabledSkills: readonly string[]): ProjectSkillBinding {
  return { enabledSkills: [...enabledSkills], defaultSkill: "", recentSkills: [], enabledPacks: [] };
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

let serverSkills: Skill[];

beforeEach(() => {
  serverSkills = [skill("s1"), skill("s2", true)];
  listSkillsMock.mockImplementation(() => Promise.resolve([...serverSkills]));
  setSkillDisabledMock.mockImplementation((skillId: string, disabled: boolean) => {
    serverSkills = serverSkills.map((item) => (item.skillId === skillId ? { ...item, disabled } : item));
    return Promise.resolve(undefined);
  });
  deleteSkillMock.mockImplementation((skillId: string) => {
    serverSkills = serverSkills.filter((item) => item.skillId !== skillId);
    return Promise.resolve(undefined);
  });
  createSkillMock.mockImplementation((draft) => {
    const created = { ...skill("s-new"), name: draft.name };
    serverSkills.push(created);
    return Promise.resolve(created);
  });
  updateSkillPromptMock.mockImplementation((draft) => {
    serverSkills = serverSkills.map((item) => (item.skillId === draft.skillId ? { ...item, name: draft.name } : item));
    return Promise.resolve({ ...skill(draft.skillId), name: draft.name });
  });
  saveBindingMock.mockImplementation((_projectId, input) =>
    Promise.resolve(binding(input.enabledSkills)),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useSkillController", () => {
  it("toggles a skill disabled flag inside the cache", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useSkillController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.skills).toHaveLength(2));

    const [init] = listSkillsMock.mock.calls[0] as [RequestInit];
    expect(init.signal).toBeInstanceOf(AbortSignal);

    await act(async () => {
      await result.current.toggle(skill("s1"));
    });
    expect(setSkillDisabledMock).toHaveBeenCalledWith("s1", true);
    expect(client.getQueryData<Skill[]>(SKILLS_QUERY_KEY)?.find((item) => item.skillId === "s1")?.disabled).toBe(true);
  });

  it("creates, updates and deletes skills with list invalidation", async () => {
    const client = createTestQueryClient();
    const { result } = renderHook(() => useSkillController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.skills).toHaveLength(2));

    await act(async () => {
      await result.current.create({ name: "新技能", description: "", systemPrompt: "提示词" });
    });
    expect(createSkillMock).toHaveBeenCalled();

    await act(async () => {
      await result.current.update({ skillId: "s1", name: "改名", description: "", systemPrompt: "x" });
    });
    expect(updateSkillPromptMock).toHaveBeenCalledWith({ skillId: "s1", name: "改名", description: "", systemPrompt: "x" });

    await act(async () => {
      await result.current.remove("s2");
    });
    expect(deleteSkillMock).toHaveBeenCalledWith("s2");
    expect(client.getQueryData<Skill[]>(SKILLS_QUERY_KEY)?.map((item) => item.skillId)).toEqual(["s1", "s-new"]);
    await waitFor(() => expect(listSkillsMock.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("tracks concurrent skill toggles independently", async () => {
    const resolvers = new Map<string, () => void>();
    setSkillDisabledMock.mockImplementation(
      (skillId: string, disabled: boolean) =>
        new Promise<void>((resolve) => {
          resolvers.set(skillId, () => {
            serverSkills = serverSkills.map((item) => (item.skillId === skillId ? { ...item, disabled } : item));
            resolve();
          });
        }),
    );
    const client = createTestQueryClient();
    const { result } = renderHook(() => useSkillController(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.skills).toHaveLength(2));

    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.toggle(skill("s1"));
      second = result.current.toggle(skill("s2", true));
    });
    await waitFor(() => {
      expect(result.current.isTogglingSkill("s1")).toBe(true);
      expect(result.current.isTogglingSkill("s2")).toBe(true);
    });

    await act(async () => {
      resolvers.get("s1")?.();
      await first;
    });
    await waitFor(() => expect(result.current.isTogglingSkill("s1")).toBe(false));
    expect(result.current.isTogglingSkill("s2")).toBe(true);

    await act(async () => {
      resolvers.get("s2")?.();
      await second;
    });
  });
});
