import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import { listProjects, normalizeProject, uploadProjectFiles } from "./projectsApi";
import { buildSimpleSkillConfig, normalizeProjectSkillBinding, normalizeSkill } from "./skillsApi";
import { addMemory, MemoryConflictError, normalizeMemoryEntry, normalizeMemoryScope, normalizeMemorySuggestion } from "./memoryApi";

function fakeClient(payload: unknown): { client: HttpClient; fetchImpl: ReturnType<typeof vi.fn> } {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }));
  return { client: new HttpClient({ fetchImpl }), fetchImpl };
}

describe("projectsApi", () => {
  it("normalizes projects with documents", () => {
    const project = normalizeProject({
      id: "proj-1",
      name: "调研",
      documents: [{ id: "d1", name: "report.pdf", fileId: "f1", projectId: "proj-1", chunkCount: 3, chunked: true }],
      updatedAt: 42,
    });
    expect(project).toMatchObject({ id: "proj-1", name: "调研", updatedAt: 42 });
    expect(project?.documents[0]).toMatchObject({ fileId: "f1", kind: "text", chunked: true });
    expect(normalizeProject({ name: "no-id" })).toBeNull();
  });

  it("lists projects via the action endpoint", async () => {
    const { client, fetchImpl } = fakeClient({ projects: [{ id: "p1", name: "A" }] });
    const projects = await listProjects(client);
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ action: "list" });
    expect(projects).toHaveLength(1);
  });

  it("uploads project files as multipart form data", async () => {
    const { client, fetchImpl } = fakeClient({ documents: [{ id: "d1", name: "a.txt", fileId: "f1" }] });
    const documents = await uploadProjectFiles("p1", [new File(["x"], "a.txt")], { ocrEnabled: true, apiKey: "k" }, client);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/project-files?projectId=p1");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Headers).get("Content-Type")).toBeNull();
    expect(documents[0]).toMatchObject({ name: "a.txt", fileId: "f1" });
  });
});

describe("skillsApi", () => {
  it("normalizes skills and bindings", () => {
    expect(normalizeSkill({ skillId: "s1", name: "技能", builtin: true, disabled: false })).toMatchObject({
      skillId: "s1",
      builtin: true,
      version: "1.0.0",
    });
    expect(normalizeSkill({ name: "no-id" })).toBeNull();
    expect(normalizeProjectSkillBinding({ enabledSkills: ["a", 1], defaultSkill: "a" })).toEqual({
      enabledSkills: ["a"],
      defaultSkill: "a",
      recentSkills: [],
      enabledPacks: [],
    });
  });

  it("buildSimpleSkillConfig fills required schema fields", () => {
    const config = buildSimpleSkillConfig({ name: "  周报助手  ", description: "写周报", systemPrompt: "你是助手" }, "custom-1");
    expect(config).toMatchObject({
      skillId: "custom-1",
      name: "周报助手",
      memoryPolicy: { scope: "none", read: false, write: false },
      projectBinding: { enabled: false },
    });
  });
});

describe("memoryApi", () => {
  it("normalizes entries and scopes", () => {
    expect(normalizeMemoryEntry({ id: "m1", content: "偏好简洁回答", category: "preference", pinned: true })).toMatchObject({
      id: "m1",
      pinned: true,
    });
    expect(normalizeMemoryEntry({ content: "" })).toBeNull();
    expect(normalizeMemoryScope("project:proj-1")).toBe("project:proj-1");
    expect(normalizeMemoryScope("evil string")).toBe("global");
    expect(normalizeMemoryScope("")).toBe("global");
  });

  it("normalizes memory suggestions with category fallback", () => {
    expect(normalizeMemorySuggestion({ content: "记住这个", category: "weird", scope: "global" })).toEqual({
      content: "记住这个",
      category: "fact",
      scope: "global",
    });
    expect(normalizeMemorySuggestion({ content: "  " })).toBeNull();
  });

  it("maps 409 conflicts to MemoryConflictError", async () => {
    const fetchImpl = vi.fn(async () => new Response(
      JSON.stringify({ error: "conflict", code: "memory_conflict", conflicts: [{ id: "old-1", content: "旧记忆" }] }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    ));
    const client = new HttpClient({ fetchImpl });
    const failure = await addMemory({ content: "新记忆" }, client).catch((reason: unknown) => reason);
    expect(failure).toBeInstanceOf(MemoryConflictError);
    expect((failure as MemoryConflictError).conflicts[0]).toMatchObject({ id: "old-1" });
  });
});
