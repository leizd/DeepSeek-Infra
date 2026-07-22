import { describe, expect, it, vi } from "vitest";

import { runUiAction } from "./runUiAction";

async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("runUiAction", () => {
  it("runs success and settled handlers", async () => {
    const onSuccess = vi.fn();
    const onSettled = vi.fn();

    runUiAction(Promise.resolve("saved"), { onSuccess, onSettled });
    await flushPromises();

    expect(onSuccess).toHaveBeenCalledWith("saved");
    expect(onSettled).toHaveBeenCalledOnce();
  });

  it("contains rejected actions and still settles", async () => {
    const onSuccess = vi.fn();
    const onSettled = vi.fn();

    runUiAction(Promise.reject(new Error("failed")), { onSuccess, onSettled });
    await flushPromises();

    expect(onSuccess).not.toHaveBeenCalled();
    expect(onSettled).toHaveBeenCalledOnce();
  });
});
