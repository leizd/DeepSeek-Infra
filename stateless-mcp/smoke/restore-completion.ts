import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import { parseToolText, RetryingMcpClient } from "../src/retrying-client.js";
import type { RestoreJournal, TaskRecord } from "../src/task-store.js";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") {
  throw new Error("native MCP process qualification is cloud-only");
}
const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repoRoot, "artifacts", "native-stateless-mcp");
const composeArguments = ["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml"];
const token = process.env.MCP_AUTH_TOKEN || "";
const backupToken = process.env.MCP_INTERNAL_BACKUP_TOKEN || "";
assert.ok(token && backupToken && token !== backupToken, "independent qualification tokens are required");
const sourceCommit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: repoRoot, encoding: "utf8" }).trim();
const sourceTree = execFileSync("git", ["rev-parse", "HEAD^{tree}"], { cwd: repoRoot, encoding: "utf8" }).trim();
assert.equal(sourceCommit, process.env.GITHUB_SHA);
assert.equal(execFileSync("git", ["status", "--porcelain", "--untracked-files=all"], { cwd: repoRoot, encoding: "utf8" }).trim(), "");
const report: Record<string, unknown> = {
  sourceCommit, sourceTree, runId: process.env.GITHUB_RUN_ID, event: process.env.GITHUB_EVENT_NAME,
  checkedAt: new Date().toISOString(), status: "FAIL", readiness: "NOT_READY",
  localNativeCompilation: false, fullMigrationQualified: false, productionProviderQualified: false,
};

function docker(args: string[], timeout = 15_000): string {
  const result = spawnSync("docker", args, { cwd: repoRoot, encoding: "utf8", timeout, stdio: "pipe" });
  assert.equal(result.status, 0, `owned Docker operation failed: ${result.error?.message || result.stderr.slice(-1200)}`);
  return result.stdout.trim();
}

const compose = (...args: string[]) => docker([...composeArguments, ...args]);
const delay = async (milliseconds: number) => await new Promise((resolve) => setTimeout(resolve, milliseconds));

async function internal(base: string, route: string, body?: unknown, headers: Record<string, string> = {}): Promise<Response> {
  const response = await fetch(base + route, {
    method: body === undefined ? "GET" : "POST", signal: AbortSignal.timeout(10_000),
    headers: { authorization: `Bearer ${backupToken}`, ...(body !== undefined && typeof body !== "string" ? { "content-type": "application/json" } : {}), ...headers },
    ...(body === undefined ? {} : { body: typeof body === "string" ? body : JSON.stringify(body) }),
  });
  assert.equal(response.status, 200, `internal ${route}: ${response.status} ${response.ok ? "" : (await response.text()).slice(0, 1200)}`);
  return response;
}

async function task(client: RetryingMcpClient, taskId: string): Promise<TaskRecord> {
  const call = await client.callTool("get_task", { taskId });
  const result = parseToolText<TaskRecord>(call.result);
  assert.equal(result.id, taskId);
  assert.ok(Number.isSafeInteger(result.attempts) && result.attempts >= 0);
  return result;
}

async function waitUntil<T>(read: () => Promise<T>, ready: (value: T) => boolean, timeout: number): Promise<T> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await read();
    if (ready(value)) return value;
    await delay(250);
  }
  throw new Error("native completion observation deadline expired");
}

async function main(): Promise<void> {
  mkdirSync(output, { recursive: true });
  docker([...composeArguments, "up", "-d", "--build"], 600_000);
  const base = "http://127.0.0.1:8011";
  await waitUntil(async () => {
    try { return (await fetch(`${base}/readyz`, { signal: AbortSignal.timeout(1000) })).ok; }
    catch { return false; }
  }, (ready) => ready, 60_000);
  const images: Record<string, string> = {};
  const processes: Record<string, string[]> = {};
  for (const instance of ["mcp-instance-1", "mcp-instance-2"]) {
    const container = compose("ps", "-q", instance);
    assert.match(container, /^[a-f0-9]{64}$/u);
    assert.equal(docker(["inspect", "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}', container]), sourceCommit);
    const image = docker(["inspect", "--format", "{{.Image}}", container]);
    assert.match(image, /^sha256:[a-f0-9]{64}$/u);
    images[instance] = image;
    const roles = docker(["top", container, "-eo", "pid,args"]).split(/\r?\n/u).slice(1)
      .map((row) => path.basename(row.trim().split(/\s+/u)[1] || ""));
    assert.ok(roles.includes("deepseek-stateless-mcp"));
    assert.ok(roles.every((role) => !/^(?:python(?:\d+(?:\.\d+)*)?|node|nodejs)$/u.test(role)));
    processes[instance] = roles;
  }
  report.images = images;
  report.observedProcesses = processes;

  // A real empty snapshot avoids changing the already-running task on restore.
  const backupId = `completion-empty-${randomUUID()}`;
  await internal(base, "/internal/backups/prepare", { backupId });
  const snapshot = await (await internal(base, `/internal/backups/${backupId}/stream`)).text();
  await internal(base, `/internal/backups/${backupId}/release`, {});
  const sourceDigest = createHash("sha256").update(snapshot).digest("hex");
  report.snapshotSha256 = sourceDigest;
  const client = new RetryingMcpClient([`${base}/mcp`], token, 1);
  const call = await client.callTool("start_test_run", {
    idempotencyKey: `completion-${randomUUID()}`,
    target: "rust/crates/deepseek-stateless-mcp/examples/failover_probe.rs",
    keyword: "native-completion-fence", timeoutSeconds: 60,
  });
  const { taskId } = parseToolText<{ taskId: string }>(call.result);
  assert.match(taskId, /^[a-f0-9-]{36}$/u);
  const running = await waitUntil(async () => await task(client, taskId), (value) => value.status === "running" && value.ownerInstance !== null, 20_000);
  const owner = running.ownerInstance;
  assert.ok(owner === "mcp-instance-1" || owner === "mcp-instance-2");
  assert.equal(running.attempts, 1);
  report.taskId = taskId;
  report.originalOwner = owner;
  const probe = "/usr/local/bin/native-failover-probe";
  const observe = async () => {
    const value: unknown = JSON.parse(compose("exec", "-T", owner, probe, "--completion-status"));
    assert.ok(value && typeof value === "object" && "started" in value && "finished" in value && "childAlive" in value);
    assert.equal(typeof value.started, "boolean");
    assert.equal(typeof value.finished, "boolean");
    assert.equal(typeof value.childAlive, "boolean");
    return value as { started: boolean; finished: boolean; childAlive: boolean };
  };
  await waitUntil(observe, (value) => value.started && value.childAlive, 10_000);
  const restoreId = `completion-restore-${randomUUID()}`;
  const transactionDigest = createHash("sha256").update(restoreId).digest("hex");
  report.restoreId = restoreId;
  const prepared = await (await internal(base, `/internal/restores/${restoreId}/prepare`, snapshot, {
    "x-transaction-digest": transactionDigest, "x-content-sha256": sourceDigest, "content-type": "application/x-ndjson",
  })).json() as RestoreJournal;
  assert.equal(prepared.restoreId, restoreId);
  assert.equal(prepared.sourceDigest, sourceDigest);
  assert.equal(prepared.phase, "prepared");
  assert.equal(prepared.records, 0);
  report.restoredSourceTaskCount = prepared.records;
  for (const action of ["commit-intent", "commit"]) {
    const journal = await (await internal(base, `/internal/restores/${restoreId}/${action}`, { transactionDigest })).json() as RestoreJournal;
    assert.equal(journal.transactionDigest, transactionDigest);
    assert.equal(journal.phase, action === "commit" ? "committed-pending-complete" : "commit-intent");
    if (action === "commit") report.restorePhaseBeforeRelease = journal.phase;
  }
  report.restoreFenceConfirmed = true;
  compose("exec", "-T", owner, probe, "--completion-release");
  await waitUntil(observe, (value) => value.finished && !value.childAlive, 10_000);
  report.originalNativeChildFinished = true;
  const childFinishedAt = Date.now();
  report.childFinishedAt = childFinishedAt;
  // Outlive the configured 5s lease while settlement is mechanically fenced.
  await delay(7_000);
  const held = await task(client, taskId);
  const heldObservedAt = Date.now();
  report.fenceHeldPastChildExitMs = heldObservedAt - childFinishedAt;
  report.configuredLeaseMs = 5_000;
  report.heldObservedAt = heldObservedAt;
  report.taskWhileFenced = held;
  const leaseStayedCurrent = held.leaseUntil !== null && held.leaseUntil > heldObservedAt;
  report.leaseStayedCurrent = leaseStayedCurrent;
  const released = await (await internal(base, `/internal/restores/${restoreId}/complete`, {})).json() as RestoreJournal;
  assert.equal(released.phase, "complete");
  report.restoreCompleted = true;
  const result = await waitUntil(async () => await task(client, taskId), (value) => value.attempts > 1 || ["succeeded", "failed"].includes(value.status), 15_000);
  report.taskAfterRelease = result;
  assert.equal(leaseStayedCurrent, true, "finished task lost its lease while completion was restore-fenced");
  assert.equal(result.attempts, 1, "completed native payload was reclaimed instead of retaining its original outcome");
  assert.equal(result.ownerInstance, owner);
  assert.equal(result.requestHash, running.requestHash);
  assert.equal(result.idempotencyKeyHash, running.idempotencyKeyHash);
  assert.equal(result.status, "succeeded");
  assert.equal(result.exitCode, 0);
  assert.equal(result.error, null);
  assert.match(result.stdout, /native completion fence probe completed/u);
  report.status = "PASS";
}

try { await main(); }
catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
} finally {
  mkdirSync(output, { recursive: true });
  report.finishedAt = new Date().toISOString();
  writeFileSync(path.join(output, "restore-completion.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report, null, 2));
}
