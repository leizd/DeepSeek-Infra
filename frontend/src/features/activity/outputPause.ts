export interface OutputPauseGate {
  readonly paused: boolean;
  pause(): void;
  resume(): void;
  waitUntilResumed(): Promise<void>;
}

export function createOutputPauseGate(): OutputPauseGate {
  let paused = false;
  const resolvers: Array<() => void> = [];
  return {
    get paused() {
      return paused;
    },
    pause() {
      paused = true;
    },
    resume() {
      paused = false;
      const pending = resolvers.splice(0, resolvers.length);
      for (const resolve of pending) resolve();
    },
    waitUntilResumed() {
      if (!paused) return Promise.resolve();
      return new Promise<void>((resolve) => {
        resolvers.push(resolve);
      });
    },
  };
}
