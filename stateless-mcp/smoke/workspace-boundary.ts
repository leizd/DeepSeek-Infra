import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import type { TaskRecord } from "../src/task-store.js";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") {
  throw new Error("native MCP filesystem qualification is cloud-only");
}
const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repoRoot, "artifacts", "native-stateless-mcp");
const git = (...args: string[]) => execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
const sourceCommit = git("rev-parse", "HEAD");
const sourceTree = git("rev-parse", "HEAD^{tree}");
assert.equal(sourceCommit, process.env.GITHUB_SHA);
assert.equal(git("status", "--porcelain", "--untracked-files=all"), "");
assert.match(process.env.GITHUB_RUN_ID || "", /^\d+$/u);
const fixtureRoot = `/tmp/native-mcp-workspace-${process.env.GITHUB_RUN_ID}`;
const workspace = `${fixtureRoot}/workspace`;
const inside = `inside-${randomUUID()}`;
const outside = `outside-${randomUUID()}`;
const rows: Record<string, unknown>[] = [];
const searches: Record<string, unknown>[] = [];
const report: Record<string, unknown> = {
  sourceCommit, sourceTree, runId: process.env.GITHUB_RUN_ID, event: process.env.GITHUB_EVENT_NAME,
  checkedAt: new Date().toISOString(), status: "FAIL", readiness: "NOT_READY",
  localNativeCompilation: false, fullMigrationQualified: false, fullSandboxQualified: false,
  fixtureOnly: true, cases: rows, searches,
};

function docker(args: string[], timeout = 15_000): string {
  const result = spawnSync("docker", args, { cwd: repoRoot, encoding: "utf8", timeout, stdio: "pipe" });
  assert.equal(result.status, 0, `owned Docker operation failed: ${result.error?.message || result.stderr.slice(-1000)}`);
  return result.stdout.trim();
}
const delay = async (milliseconds: number) => await new Promise((resolve) => setTimeout(resolve, milliseconds));
let container: string | undefined;
let temporary: string | undefined;

function tool(name: string, arguments_: Record<string, unknown>): { isError?: boolean; content: { text: string }[] } {
  assert.ok(container);
  const wire = JSON.parse(docker(["exec", container, "curl", "--fail", "--silent", "--show-error", "--max-time", "3",
    "-H", "content-type: application/json", "--data-binary",
    JSON.stringify({ jsonrpc: "2.0", id: randomUUID(), method: "tools/call", params: { name, arguments: arguments_ } }),
    "http://127.0.0.1:8010/mcp"]));
  assert.equal(wire.error, undefined);
  return wire.result;
}

function invocationCount(): number {
  assert.ok(container);
  const text = docker(["exec", container, "cat", `${fixtureRoot}/invocations.log`]);
  return text ? text.split(/\r?\n/u).length : 0;
}

async function task(target: string): Promise<TaskRecord> {
  const result = tool("start_test_run", { idempotencyKey: `workspace-${randomUUID()}`, target,
    keyword: "native-workspace-boundary", timeoutSeconds: 5 });
  assert.notEqual(result.isError, true, "retained task admission must return the durable task identity");
  const { taskId } = JSON.parse(result.content[0]!.text);
  assert.equal(typeof taskId, "string");
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    const response = tool("get_task", { taskId });
    assert.notEqual(response.isError, true);
    const value = JSON.parse(response.content[0]!.text) as TaskRecord;
    assert.equal(value.id, taskId);
    if (["succeeded", "failed", "cancelled"].includes(value.status)) return value;
    await delay(100);
  }
  throw new Error("actual native task did not settle within the qualification deadline");
}

async function main(): Promise<void> {
  const compose = ["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml"];
  const original = docker([...compose, "ps", "-q", "mcp-instance-1"]);
  assert.match(original, /^[a-f0-9]{64}$/u);
  const image = docker(["inspect", "--format", "{{.Image}}", original]);
  assert.match(image, /^sha256:[a-f0-9]{64}$/u);
  const metadata = JSON.parse(docker(["image", "inspect", image]))[0];
  assert.equal(metadata.Config.Labels["org.opencontainers.image.revision"], sourceCommit);
  const network = `${process.env.COMPOSE_PROJECT_NAME}_default`;
  report.imageId = image;
  report.revisionLabel = metadata.Config.Labels["org.opencontainers.image.revision"];
  report.workspace = workspace;
  report.fixtureInside = inside;
  report.fixtureOutside = outside;
  temporary = mkdtempSync(path.join(os.tmpdir(), "native-mcp-workspace-"));
  const tree = path.join(temporary, "fixture");
  mkdirSync(path.join(tree, "workspace", "nested"), { recursive: true });
  mkdirSync(path.join(tree, "workspace-extra"));
  chmodSync(tree, 0o777);
  writeFileSync(path.join(tree, "workspace", "allowed.txt"), inside);
  writeFileSync(path.join(tree, "workspace", "--leading.txt"), inside);
  writeFileSync(path.join(tree, "outside.txt"), outside);
  writeFileSync(path.join(tree, "workspace-extra", "outside.txt"), outside);
  writeFileSync(path.join(tree, "invocations.log"), "", { mode: 0o666 });
  chmodSync(path.join(tree, "invocations.log"), 0o666);
  symlinkSync("../outside.txt", path.join(tree, "workspace", "escaped-link.txt"));
  symlinkSync("allowed.txt", path.join(tree, "workspace", "inside-link.txt"));
  container = docker(["create", "--network", network, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--memory", "256m", "--pids-limit", "64", "--label", `native-mcp-workspace-run=${process.env.GITHUB_RUN_ID}`,
    "-e", "REDIS_URL=redis://redis:6379", "-e", `REDIS_PREFIX=native-workspace-${process.env.GITHUB_RUN_ID}`,
    "-e", `MCP_INSTANCE_ID=workspace-${process.env.GITHUB_RUN_ID}`, "-e", `MCP_WORKSPACE_ROOT=${workspace}`,
    "-e", "MCP_TASK_PROGRAM=/usr/local/bin/native-failover-probe", "-e", "MCP_TASK_POLL_MS=50",
    "-e", "MCP_TASK_LEASE_MS=5000", "-e", `MCP_BOUNDARY_INVOCATIONS=${fixtureRoot}/invocations.log`, image]);
  assert.match(container, /^[a-f0-9]{64}$/u);
  docker(["cp", tree, `${container}:${fixtureRoot}`]);
  docker(["start", container]);
  const deadline = Date.now() + 10_000;
  let ready = false;
  do {
    const result = spawnSync("docker", ["exec", container, "curl", "--fail", "--silent", "--show-error", "--max-time", "1",
      "http://127.0.0.1:8010/readyz"], { cwd: repoRoot, encoding: "utf8", timeout: 3000 });
    ready = result.status === 0;
    if (!ready) await delay(150);
  } while (!ready && Date.now() < deadline);
  assert.ok(ready);
  const state = JSON.parse(docker(["inspect", container]))[0];
  assert.deepEqual(state.Mounts, []);
  assert.ok(!state.HostConfig.PortBindings || Object.keys(state.HostConfig.PortBindings).length === 0);
  const roles = docker(["top", container, "-eo", "pid,args"]).split(/\r?\n/u).slice(1)
    .map((row) => path.basename(row.trim().split(/\s+/u)[1] || ""));
  assert.deepEqual(roles, ["deepseek-stateless-mcp"]);
  report.observedProcesses = roles;
  report.hostMounts = 0;
  report.publishedPorts = 0;

  const specifications = [
    { name: "relative-file", target: "allowed.txt", allowed: true },
    { name: "normalized-descendant", target: "nested/../allowed.txt", allowed: true },
    { name: "absolute-inside", target: `${workspace}/allowed.txt`, allowed: true },
    { name: "leading-option-filename", target: "--leading.txt", allowed: true },
    { name: "test-selector", target: "allowed.txt::synthetic_case", allowed: true },
    { name: "inside-symlink", target: "inside-link.txt", allowed: true },
    { name: "parent-traversal", target: "../outside.txt", allowed: false },
    { name: "absolute-outside", target: `${fixtureRoot}/outside.txt`, allowed: false },
    { name: "sibling-prefix", target: `${fixtureRoot}/workspace-extra/outside.txt`, allowed: false },
    { name: "outside-symlink", target: "escaped-link.txt", allowed: false },
  ];
  for (const specification of specifications) {
    const before = invocationCount();
    const result = await task(specification.target);
    const after = invocationCount();
    assert.equal(result.attempts, 1);
    const row: Record<string, unknown> = { ...specification, before, after, task: result };
    rows.push(row);
    if (specification.allowed) {
      assert.equal(result.status, "succeeded");
      assert.equal(result.error, null);
      assert.equal(result.exitCode, 0);
      const observed = JSON.parse(result.stdout);
      assert.equal(observed.contents, inside);
      assert.equal(after, before + 1);
      row.actualTarget = observed.target;
      row.argumentAbsolute = path.posix.isAbsolute(observed.target);
      if (specification.name === "test-selector") assert.equal(observed.selector, "synthetic_case");
    } else {
      row.deniedBeforeChildSpawn = result.status === "failed" && result.stdout === "" &&
        /within the configured workspace/u.test(result.error || "") && after === before;
      row.outsideFixtureRead = result.stdout.includes(outside);
    }
  }
  for (const specification of [{ name: "inside", target: "allowed.txt", allowed: true },
    { name: "outside-symlink", target: "escaped-link.txt", allowed: false }]) {
    const result = tool("code_search", { query: specification.allowed ? inside : outside, path: specification.target });
    const text = result.content.map((item) => item.text).join("\n");
    const row = { ...specification, result, outsideFixtureRead: !specification.allowed && text.includes(outside),
      denied: !specification.allowed && result.isError === true && /within the configured workspace/u.test(text) };
    searches.push(row);
    if (specification.allowed) {
      assert.notEqual(result.isError, true);
      assert.ok(text.includes(inside), "the original code-search control must read the inside fixture");
    }
  }
  assert.ok(rows.every((row) => row.allowed ? row.argumentAbsolute === true : row.deniedBeforeChildSpawn === true) &&
    searches.every((row) => row.allowed || row.denied), "native file access crossed the configured workspace boundary");
  report.status = "PASS";
  report.taskWorkspaceBoundaryQualified = true;
}

mkdirSync(output, { recursive: true });
try {
  await main();
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  throw error;
} finally {
  writeFileSync(path.join(output, "workspace-boundary.json"), `${JSON.stringify(report, null, 2)}\n`);
  if (container) docker(["rm", "--force", container]);
  if (temporary) {
    assert.equal(path.dirname(temporary), path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith("native-mcp-workspace-"));
    rmSync(temporary, { recursive: true, force: true });
  }
  writeFileSync(path.join(output, "workspace-boundary.json"), `${JSON.stringify(report, null, 2)}\n`);
}
