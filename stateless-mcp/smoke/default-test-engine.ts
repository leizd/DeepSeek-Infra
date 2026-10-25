import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { loadConfig } from "../src/config.js";
import { MemoryTaskStore } from "../src/memory-task-store.js";
import { executeTestTask } from "../src/test-runner.js";
import type { TaskRecord, TestTaskArguments } from "../src/task-store.js";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") {
  throw new Error("stock native test-tool qualification and Python oracle are cloud-only");
}
const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repoRoot, "artifacts", "native-stateless-mcp");
const git = (...args: string[]) => execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
const sourceCommit = git("rev-parse", "HEAD");
const sourceTree = git("rev-parse", "HEAD^{tree}");
assert.equal(sourceCommit, process.env.GITHUB_SHA);
assert.equal(git("status", "--porcelain", "--untracked-files=all"), "");
assert.match(process.env.GITHUB_RUN_ID || "", /^\d+$/u);
const report: Record<string, unknown> = {
  sourceCommit, sourceTree, runId: process.env.GITHUB_RUN_ID, event: process.env.GITHUB_EVENT_NAME,
  checkedAt: new Date().toISOString(), status: "FAIL", readiness: "NOT_READY",
  productionTarget: "production", localNativeCompilation: false, fullMigrationQualified: false,
  PythonUsedOnlyByOfflineOriginalOracle: true, defaultNativeTestToolQualified: false,
};
let container: string | undefined;
let temporary: string | undefined;
const delay = async (milliseconds: number) => await new Promise((resolve) => setTimeout(resolve, milliseconds));

function docker(args: string[], timeout = 15_000): string {
  const result = spawnSync("docker", args, { cwd: repoRoot, encoding: "utf8", timeout, stdio: "pipe" });
  assert.equal(result.status, 0, `owned Docker operation failed: ${result.error?.message || result.stderr.slice(-1200)}`);
  return result.stdout.trim();
}

function call(name: string, arguments_: Record<string, unknown>): unknown {
  assert.ok(container);
  const rpc = JSON.parse(docker(["exec", container, "curl", "--fail", "--silent", "--show-error", "--max-time", "2",
    "-H", "content-type: application/json", "--data-binary",
    JSON.stringify({ jsonrpc: "2.0", id: randomUUID(), method: "tools/call", params: { name, arguments: arguments_ } }),
    "http://127.0.0.1:8010/mcp"]));
  assert.equal(rpc.error, undefined);
  assert.notEqual(rpc.result.isError, true);
  return JSON.parse(rpc.result.content[0].text);
}

async function main(): Promise<void> {
  assert.ok(temporary);
  const fixture = "def test_kept_success():\n    assert 2 + 2 == 4\n\ndef test_excluded_failure():\n    raise AssertionError('selector control must exclude this test')\n";
  const file = path.join(temporary, "test_retained_feature.py");
  writeFileSync(file, fixture);
  const arguments_: TestTaskArguments = {
    target: "test_retained_feature.py::test_kept_success", keyword: "kept_success", markers: "not slow", timeoutSeconds: 10,
  };
  // This is the retained original executor, used only as an offline oracle on
  // the CI host. Neither Python nor this reference is copied into production.
  const reference = new MemoryTaskStore();
  const { task } = await reference.createOrGet({ idempotencyKey: `offline-oracle-${randomUUID()}`, arguments: arguments_, now: Date.now() });
  const oracle = await executeTestTask(task, { ...loadConfig(), workspaceRoot: temporary, taskTimeoutSeconds: 10 });
  report.offlineOriginalOracle = oracle;
  report.originalExecutorSourceSha256 = createHash("sha256").update(readFileSync(path.join(repoRoot, "stateless-mcp/src/test-runner.ts"))).digest("hex");
  assert.equal(oracle.error, null);
  assert.equal(oracle.exitCode, 0);
  assert.match(oracle.stdout, /1 passed/u);
  assert.ok(!oracle.stdout.includes("1 failed"));
  report.originalOraclePassed = true;
  report.arguments = arguments_;
  report.fixtureSha256 = createHash("sha256").update(fixture).digest("hex");

  const original = JSON.parse(readFileSync(path.join(output, "default-durability.json"), "utf8"));
  assert.equal(original.sourceCommit, sourceCommit);
  assert.equal(original.sourceTree, sourceTree);
  assert.equal(original.runId, process.env.GITHUB_RUN_ID);
  assert.equal(original.status, "PASS");
  const image = original.imageId as string;
  assert.match(image, /^sha256:[a-f0-9]{64}$/u);
  const metadata = JSON.parse(docker(["image", "inspect", image]))[0];
  assert.equal(metadata.Config.Labels["org.opencontainers.image.revision"], sourceCommit);
  assert.deepEqual(metadata.Config.Cmd, ["deepseek-stateless-mcp"]);
  assert.ok(!metadata.Config.Env.some((value: string) => value.startsWith("MCP_TASK_PROGRAM=")));
  report.imageId = image;
  report.revisionLabel = sourceCommit;
  const compose = ["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml"];
  const redis = docker([...compose, "ps", "-q", "redis"]);
  assert.match(redis, /^[a-f0-9]{64}$/u);
  const redisState = JSON.parse(docker(["inspect", redis]))[0];
  const networks = Object.keys(redisState.NetworkSettings.Networks);
  assert.deepEqual(networks, [`${process.env.COMPOSE_PROJECT_NAME}_default`]);
  container = docker(["run", "--detach", "--network", networks[0]!, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--memory", "256m", "--pids-limit", "64", "--label", `native-mcp-default-engine-run=${process.env.GITHUB_RUN_ID}`,
    "-e", "REDIS_URL=redis://redis:6379", "-e", `REDIS_PREFIX=native-engine:${process.env.GITHUB_RUN_ID}:${randomUUID()}`,
    "-e", "MCP_TASK_POLL_MS=50", image]);
  assert.match(container, /^[a-f0-9]{64}$/u);
  const deadline = Date.now() + 10_000;
  let ready = false;
  do {
    const result = spawnSync("docker", ["exec", container, "curl", "--fail", "--silent", "--max-time", "1", "http://127.0.0.1:8010/readyz"],
      { cwd: repoRoot, encoding: "utf8", timeout: 3000 });
    ready = result.status === 0;
    if (!ready) await delay(100);
  } while (!ready && Date.now() < deadline);
  assert.ok(ready, "stock native service did not become ready through actual Redis");
  const state = JSON.parse(docker(["inspect", container]))[0];
  assert.equal(state.Image, image);
  assert.deepEqual(state.Mounts, []);
  assert.ok(!state.HostConfig.PortBindings || Object.keys(state.HostConfig.PortBindings).length === 0);
  assert.ok(!state.Config.Env.some((value: string) => value.startsWith("MCP_TASK_PROGRAM=")));
  assert.deepEqual(docker(["exec", container, "ls", "/usr/local/bin"]).split(/\r?\n/u), ["deepseek-stateless-mcp"]);
  const programs = spawnSync("docker", ["exec", container, "sh", "-c",
    "command -v python || command -v python3 || command -v node || command -v native-failover-probe"], { cwd: repoRoot, encoding: "utf8", timeout: 3000 });
  assert.notEqual(programs.status, 0);
  assert.equal(programs.error, undefined);
  assert.equal(programs.stdout.trim(), "");
  report.productionPythonNodeAndFixtureAbsent = true;
  report.mounts = 0;
  report.publishedPorts = 0;
  docker(["cp", file, `${container}:/workspace/test_retained_feature.py`]);
  assert.equal(docker(["exec", container, "sha256sum", "/workspace/test_retained_feature.py"]).split(/\s+/u)[0], report.fixtureSha256);
  report.fixtureCopiedAndIndependentlyHashed = true;
  const created = call("start_test_run", { idempotencyKey: `native-default-engine-${randomUUID()}`, ...arguments_ }) as { taskId: string };
  let native: TaskRecord | undefined;
  const taskDeadline = Date.now() + 15_000;
  do {
    native = call("get_task", { taskId: created.taskId }) as TaskRecord;
    assert.ok(native);
    if (["succeeded", "failed"].includes(native.status)) break;
    await delay(100);
  } while (Date.now() < taskDeadline);
  assert.ok(native);
  assert.deepEqual(native.arguments, arguments_);
  assert.equal(native.attempts, 1);
  report.nativeTask = native;
  report.observedProcesses = docker(["top", container, "-eo", "pid,args"]).split(/\r?\n/u).slice(1)
    .map((row) => path.basename(row.trim().split(/\s+/u)[1] || ""));
  assert.deepEqual(report.observedProcesses, ["deepseek-stateless-mcp"]);
  assert.ok(native.status === "succeeded" && native.exitCode === 0 && native.error === null && /1 passed/u.test(native.stdout),
    "stock native test tool failed the retained offline pytest success control");
  report.defaultNativeTestToolQualified = true;
  report.status = "PASS";
}

mkdirSync(output, { recursive: true });
temporary = mkdtempSync(path.join(os.tmpdir(), "native-mcp-default-engine-"));
try {
  await main();
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  throw error;
} finally {
  try {
    if (container) docker(["rm", "--force", "--volumes", container]);
    report.ownedContainerRemoved = true;
  } finally {
    writeFileSync(path.join(output, "default-test-engine.json"), `${JSON.stringify(report, null, 2)}\n`);
    assert.equal(path.dirname(temporary), path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith("native-mcp-default-engine-"));
    rmSync(temporary, { recursive: true, force: true });
  }
}
