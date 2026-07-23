// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useMemoryWriteBarrier } from "./useMemoryWriteBarrier";

type Barrier = ReturnType<typeof useMemoryWriteBarrier>;

let rootBarrier: Barrier | null = null;
let drawerBarrier: Barrier | null = null;

function RootConsumer() {
  rootBarrier = useMemoryWriteBarrier();
  return null;
}

function DrawerConsumer() {
  drawerBarrier = useMemoryWriteBarrier();
  return null;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((onResolve) => {
    resolve = onResolve;
  });
  return { promise, resolve };
}

function Harness({ drawerOpen }: { drawerOpen: boolean }) {
  return (
    <>
      <RootConsumer />
      {drawerOpen && <DrawerConsumer />}
    </>
  );
}

beforeEach(() => {
  rootBarrier = null;
  drawerBarrier = null;
});

afterEach(() => {
  cleanup();
});

describe("memory write barrier", () => {
  it("shares a root save blocker with the lazy drawer and preserves its lifecycle across remount", async () => {
    const client = new QueryClient();
    const pending = deferred<void>();
    const view = render(
      <QueryClientProvider client={client}>
        <Harness drawerOpen />
      </QueryClientProvider>,
    );

    const save = rootBarrier!.runWrite("memory-save:intent", "save", "intent", () => pending.promise);
    const firstConflict = await drawerBarrier!.runClear(async () => undefined);
    expect(firstConflict.status).toBe("conflict");
    if (firstConflict.status !== "conflict") throw new Error("expected a clear conflict");
    expect(firstConflict.blocker).toMatchObject({
      entityKey: "memory-save:intent",
      operation: "save",
      intentKey: "intent",
      source: "local-lock",
    });

    view.rerender(
      <QueryClientProvider client={client}>
        <Harness drawerOpen={false} />
      </QueryClientProvider>,
    );
    view.rerender(
      <QueryClientProvider client={client}>
        <Harness drawerOpen />
      </QueryClientProvider>,
    );
    const remountedConflict = await drawerBarrier!.runClear(async () => undefined);
    expect(remountedConflict.status).toBe("conflict");
    if (remountedConflict.status !== "conflict") throw new Error("expected a remounted clear conflict");
    expect(remountedConflict.blocker.lifecycleId).toBe(firstConflict.blocker.lifecycleId);

    await act(async () => {
      pending.resolve();
      await expect(save).resolves.toMatchObject({ status: "executed" });
    });
  });

  it("shares a drawer clear blocker with the root provider without MutationCache metadata", async () => {
    const client = new QueryClient();
    const pending = deferred<void>();
    render(
      <QueryClientProvider client={client}>
        <Harness drawerOpen />
      </QueryClientProvider>,
    );

    const clear = drawerBarrier!.runClear(() => pending.promise);
    const conflict = await rootBarrier!.runWrite(
      "memory-save:later",
      "save",
      "later",
      async () => undefined,
    );
    expect(conflict.status).toBe("conflict");
    if (conflict.status !== "conflict") throw new Error("expected a save conflict");
    expect(conflict.blocker).toMatchObject({
      entityKey: "memory-list:clear",
      operation: "clear",
      intentKey: "clear",
      source: "local-lock",
    });
    expect(client.getMutationCache().getAll()).toHaveLength(0);

    await act(async () => {
      pending.resolve();
      await expect(clear).resolves.toMatchObject({ status: "executed" });
    });
  });
});
