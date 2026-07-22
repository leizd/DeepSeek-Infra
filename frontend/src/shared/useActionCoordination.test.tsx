// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { PropsWithChildren } from "react";

import type { LifecycleMutationMeta } from "../app/mutationLifecycle";
import { useActionCoordination } from "./useActionCoordination";

afterEach(cleanup);

describe("useActionCoordination", () => {
  it("automatically expires a conflict when its blocking mutation settles", async () => {
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
        { status: "conflict", activeOperation: "upload" },
        "project:p1",
        "remove",
      )).toThrow();
    });
    await waitFor(() => expect(result.current.coordinationError).toContain("正在上传"));

    await act(async () => {
      release();
      await action;
    });
    await waitFor(() => expect(result.current.coordinationError).toBe(""));
  });
});
