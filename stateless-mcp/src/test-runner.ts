import { spawn } from "node:child_process";
import { once } from "node:events";

import type { ServiceConfig } from "./config.js";
import type { TaskOutcome, TaskRecord, TaskStore } from "./task-store.js";
import { RestoreFenceError } from "./task-store.js";
import { resolveWorkspacePath } from "./workspace.js";

export const RESTORE_FENCE_RETRY_LIMIT_MS = 5 * 60 * 1000;

export async function completeWhenUnfenced(
  store: TaskStore,
  taskId: string,
  instanceId: string,
  outcome: TaskOutcome,
  pollMs: number,
  retryLimitMs = RESTORE_FENCE_RETRY_LIMIT_MS,
): Promise<void> {
  const deadline = Date.now() + retryLimitMs;
  for (;;) {
    try {
      await store.complete(taskId, instanceId, outcome, Date.now());
      return;
    } catch (error) {
      if (!(error instanceof RestoreFenceError) || Date.now() >= deadline) {
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
  }
}

function boundedAppend(current: string, chunk: string, limit: number): string {
  const currentBytes = Buffer.byteLength(current);
  if (currentBytes >= limit) {
    return current;
  }
  return current + Buffer.from(chunk).subarray(0, limit - currentBytes).toString("utf8");
}

export async function executeTestTask(
  task: TaskRecord,
  config: ServiceConfig,
  signal?: AbortSignal,
): Promise<TaskOutcome> {
  const target = resolveWorkspacePath(config.workspaceRoot, task.arguments.target);
  const arguments_ = ["-m", "pytest", target, "--no-cov", "-q", "-p", "no:cacheprovider"];
  if (task.arguments.keyword !== undefined) {
    arguments_.push("-k", task.arguments.keyword);
  }
  if (task.arguments.markers !== undefined) {
    arguments_.push("-m", task.arguments.markers);
  }
  const child = spawn(process.env.PYTHON_EXECUTABLE || "python", arguments_, {
    cwd: config.workspaceRoot,
    env: { ...process.env, PYTHONHASHSEED: "0" },
    shell: false,
    ...(signal === undefined ? {} : { signal }),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    stdout = boundedAppend(stdout, chunk, config.maxOutputBytes);
  });
  child.stderr.on("data", (chunk: string) => {
    stderr = boundedAppend(stderr, chunk, config.maxOutputBytes);
  });
  const timeoutMs = Math.min(task.arguments.timeoutSeconds, config.taskTimeoutSeconds) * 1_000;
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    child.kill();
  }, timeoutMs);
  try {
    const [exitCode] = (await once(child, "close")) as [number | null];
    return {
      stdout,
      stderr,
      exitCode,
      error: timedOut ? `test run exceeded ${String(timeoutMs / 1_000)} seconds` : null,
    };
  } catch (error) {
    return {
      stdout,
      stderr,
      exitCode: null,
      error: error instanceof Error ? error.message : String(error),
    };
  } finally {
    clearTimeout(timeout);
  }
}

export class TaskWorker {
  private stopped = false;
  private activeAbort: AbortController | null = null;

  constructor(
    private readonly store: TaskStore,
    private readonly config: ServiceConfig,
  ) {}

  async start(): Promise<void> {
    while (!this.stopped) {
      try {
        const task = await this.store.claim(this.config.instanceId, Date.now(), this.config.leaseMs);
        if (task !== null) {
          await this.runClaimed(task);
          continue;
        }
      } catch (error) {
        console.error("task_worker_iteration_failed", error);
      }
      if (!this.stopped) {
        await new Promise((resolve) => setTimeout(resolve, this.config.pollMs));
      }
    }
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.activeAbort?.abort();
  }

  private async runClaimed(task: TaskRecord): Promise<void> {
    let leaseOwned = true;
    const abortController = new AbortController();
    this.activeAbort = abortController;
    const heartbeat = setInterval(() => {
      void this.store
        .heartbeat(task.id, this.config.instanceId, Date.now(), this.config.leaseMs)
        .then((owned) => {
          leaseOwned = owned;
          if (!owned) {
            abortController.abort();
          }
        })
        .catch((error: unknown) => {
          leaseOwned = false;
          abortController.abort();
          console.error("task_heartbeat_failed", { taskId: task.id, error });
        });
    }, Math.max(250, Math.floor(this.config.leaseMs / 3)));
    heartbeat.unref();
    try {
      const outcome = await executeTestTask(task, this.config, abortController.signal);
      if (leaseOwned && !this.stopped) {
        await completeWhenUnfenced(this.store, task.id, this.config.instanceId, outcome, this.config.pollMs);
      }
    } finally {
      clearInterval(heartbeat);
      this.activeAbort = null;
    }
  }
}
