import { describe, expect, it } from "vitest";

import { createOutputPauseGate } from "./outputPause";

describe("createOutputPauseGate", () => {
  it("passes through while running", async () => {
    const gate = createOutputPauseGate();
    expect(gate.paused).toBe(false);
    await expect(gate.waitUntilResumed()).resolves.toBeUndefined();
  });

  it("blocks while paused and releases all waiters on resume", async () => {
    const gate = createOutputPauseGate();
    gate.pause();
    let released = 0;
    const first = gate.waitUntilResumed().then(() => {
      released += 1;
    });
    const second = gate.waitUntilResumed().then(() => {
      released += 1;
    });
    gate.resume();
    await Promise.all([first, second]);
    expect(released).toBe(2);
    expect(gate.paused).toBe(false);
    await expect(gate.waitUntilResumed()).resolves.toBeUndefined();
  });

  it("can pause again after resuming", async () => {
    const gate = createOutputPauseGate();
    gate.pause();
    gate.resume();
    gate.pause();
    let released = false;
    const waiter = gate.waitUntilResumed().then(() => {
      released = true;
    });
    gate.resume();
    await waiter;
    expect(released).toBe(true);
  });
});
