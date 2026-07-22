// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { PropsWithChildren } from "react";

import type { LifecycleMutationMeta } from "../app/mutationLifecycle";
import { useActionCoordination } from "./useActionCoordination";

afterEach(cleanup);

describe("useActionCoordination", () => {
  it("keeps a cross-entity conflict until its exact blocker settles", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useActionCoordination(), { wrapper });
    let release!: () => void;
    const meta: LifecycleMutationMeta = {
      owner: "project-list",
      lifecycleId: "upload-p1",
      entityKey: "project:p1",
      operation: "upload",
      intentKey: "a.txt",
    };
    const mutation = client.getMutationCache().build(client, {
      mutationKey: ["projects", "upload"],
      meta,
      mutationFn: () => new Promise<void>((resolve) => {
        release = resolve;
      }),
    });

    let action!: Promise<void>;
    act(() => {
      action = mutation.execute(undefined);
    });
    await waitFor(() => expect(mutation.state.status).toBe("pending"));
    act(() => {
      expect(() => result.current.resolveAction(
        {
          status: "conflict",
          blocker: {
            lifecycleId: meta.lifecycleId,
            entityKey: meta.entityKey,
            operation: meta.operation,
            intentKey: meta.intentKey,
            source: "mutation-cache",
          },
        },
        "project-binding:p1",
        "save",
      )).toThrow();
    });
    await waitFor(() => {
      expect(result.current.coordinationError).toContain("正在上传");
      expect(result.current.coordinationFailure).toMatchObject({
        requestedEntityKey: "project-binding:p1",
        blocker: { lifecycleId: "upload-p1", entityKey: "project:p1" },
      });
    });

    await act(async () => {
      release();
      await action;
    });
    await waitFor(() => expect(result.current.coordinationError).toBe(""));
  });

  it("does not let a new mutation on the same entity impersonate the old blocker", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useActionCoordination(), { wrapper });
    let releaseUpload!: () => void;
    let releaseRename!: () => void;
    const uploadMeta: LifecycleMutationMeta = {
      owner: "project-list",
      lifecycleId: "upload-old",
      entityKey: "project:p1",
      operation: "upload",
      intentKey: "a.txt",
    };
    const upload = client.getMutationCache().build(client, {
      meta: uploadMeta,
      mutationFn: () => new Promise<void>((resolve) => { releaseUpload = resolve; }),
    });
    const uploadAction = upload.execute(undefined);
    await waitFor(() => expect(upload.state.status).toBe("pending"));
    act(() => {
      expect(() => result.current.resolveAction({
        status: "conflict",
        blocker: {
          lifecycleId: uploadMeta.lifecycleId,
          entityKey: uploadMeta.entityKey,
          operation: uploadMeta.operation,
          source: "mutation-cache",
        },
      }, "project:p1", "remove")).toThrow();
    });
    await waitFor(() => expect(result.current.coordinationError).toContain("正在上传"));

    await act(async () => {
      releaseUpload();
      await uploadAction;
    });
    const rename = client.getMutationCache().build(client, {
      meta: { ...uploadMeta, lifecycleId: "rename-new", operation: "rename", intentKey: "new" },
      mutationFn: () => new Promise<void>((resolve) => { releaseRename = resolve; }),
    });
    const renameAction = rename.execute(undefined);
    await waitFor(() => expect(rename.state.status).toBe("pending"));
    await waitFor(() => expect(result.current.coordinationError).toBe(""));

    await act(async () => {
      releaseRename();
      await renameAction;
    });
  });
});
