// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";

import { stableIntentKey } from "./mutationIntents";
import { useMutationActivity } from "./mutationLifecycle";

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("mutation lifecycle utilities", () => {
  it("keeps a scope-paused mutation visible as active", async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    let release!: () => void;
    const blocker = client.getMutationCache().build(client, {
      mutationKey: ["blocker"],
      scope: { id: "shared-scope" },
      mutationFn: () => new Promise<void>((resolve) => {
        release = resolve;
      }),
    });
    const queuedFn = vi.fn().mockResolvedValue(undefined);
    const queued = client.getMutationCache().build(client, {
      mutationKey: ["queued"],
      scope: { id: "shared-scope" },
      mutationFn: queuedFn,
    });

    const blockerPromise = blocker.execute(undefined);
    const queuedPromise = queued.execute(undefined);
    await waitFor(() => expect(queued.state.isPaused).toBe(true));
    const { result } = renderHook(() => useMutationActivity(["queued"]), { wrapper: wrapperFor(client) });
    expect(result.current).toEqual({ active: true, count: 1 });
    expect(queuedFn).not.toHaveBeenCalled();

    await act(async () => {
      release();
      await Promise.all([blockerPromise, queuedPromise]);
    });
    await waitFor(() => expect(result.current.active).toBe(false));
  });

  it("serializes object keys deterministically", () => {
    expect(stableIntentKey({ beta: 2, alpha: { delta: 4, gamma: 3 } })).toBe(
      stableIntentKey({ alpha: { gamma: 3, delta: 4 }, beta: 2 }),
    );
  });
});
