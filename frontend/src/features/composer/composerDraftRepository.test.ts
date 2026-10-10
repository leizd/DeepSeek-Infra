import { beforeEach, describe, expect, it } from "vitest";

import {
  forgetDraft,
  markPersisted,
  recallDraft,
  rememberDraft,
  resetComposerDraftRepositoryForTests,
} from "./composerDraftRepository";

const scopeA = { conversationId: "conversation-a", projectId: null };
const scopeB = { conversationId: "conversation-a", projectId: "project-b" };

beforeEach(() => {
  resetComposerDraftRepositoryForTests();
});

describe("composer draft memory repository", () => {
  it("remembers input synchronously as a dirty entry", () => {
    rememberDraft(scopeA, { text: "hello", updatedAt: 10 });
    expect(recallDraft(scopeA)).toEqual({
      text: "hello",
      updatedAt: 10,
      persistedRevision: null,
      dirty: true,
    });
  });

  it("marks a successful write as persisted, keeping the revision", () => {
    rememberDraft(scopeA, { text: "hello", updatedAt: 10 });
    markPersisted(scopeA, "10");
    expect(recallDraft(scopeA)).toMatchObject({ dirty: false, persistedRevision: "10" });
    // 重复 remember 相同且已落盘的文本是 no-op，不会重新标脏。
    rememberDraft(scopeA, { text: "hello", updatedAt: 10 });
    expect(recallDraft(scopeA)).toMatchObject({ dirty: false, persistedRevision: "10" });
  });

  it("keeps the last persisted revision when newer input arrives", () => {
    rememberDraft(scopeA, { text: "v1", updatedAt: 1 });
    markPersisted(scopeA, "1");
    rememberDraft(scopeA, { text: "v2", updatedAt: 2 });
    expect(recallDraft(scopeA)).toMatchObject({ text: "v2", dirty: true, persistedRevision: "1" });
  });

  it("scopes entries independently by conversation and project", () => {
    rememberDraft(scopeA, { text: "none project" });
    rememberDraft(scopeB, { text: "project b" });
    expect(recallDraft(scopeA)?.text).toBe("none project");
    expect(recallDraft(scopeB)?.text).toBe("project b");
    forgetDraft(scopeA);
    expect(recallDraft(scopeA)).toBeNull();
    expect(recallDraft(scopeB)?.text).toBe("project b");
  });

  it("forgets entries and resets fully for tests", () => {
    rememberDraft(scopeA, { text: "x" });
    forgetDraft(scopeA);
    expect(recallDraft(scopeA)).toBeNull();
    rememberDraft(scopeA, { text: "y" });
    rememberDraft(scopeB, { text: "z" });
    resetComposerDraftRepositoryForTests();
    expect(recallDraft(scopeA)).toBeNull();
    expect(recallDraft(scopeB)).toBeNull();
  });

  it("returns copies so callers cannot mutate stored entries", () => {
    rememberDraft(scopeA, { text: "x", updatedAt: 1 });
    const entry = recallDraft(scopeA);
    expect(entry).not.toBeNull();
    if (entry) entry.dirty = false;
    expect(recallDraft(scopeA)?.dirty).toBe(true);
  });
});
