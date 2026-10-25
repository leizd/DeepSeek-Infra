import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { randomUUID, createHash } from "node:crypto";
import { chmodSync, closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import type { TaskRecord } from "../src/task-store.js";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") {
  throw new Error("native MCP descendant qualification is cloud-only");
}
const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repoRoot, "artifacts", "native-stateless-mcp");
const git = (...args: string[]) => execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
const sourceCommit = git("rev-parse", "HEAD");
const sourceTree = git("rev-parse", "HEAD^{tree}");
assert.equal(sourceCommit, process.env.GITHUB_SHA);
assert.equal(git("status", "--porcelain", "--untracked-files=all"), "");
assert.match(process.env.GITHUB_RUN_ID || "", /^\d+$/u);
const rows: Record<string, unknown>[] = [];
const report: Record<string, unknown> = {
  sourceCommit, sourceTree, runId: process.env.GITHUB_RUN_ID, event: process.env.GITHUB_EVENT_NAME,
  checkedAt: new Date().toISOString(), status: "FAIL", readiness: "NOT_READY",
  fixtureOnly: true, fullSandboxQualified: false, fullMigrationQualified: false,
  localNativeCompilation: false, cases: rows,
};
type Identity = { pid: number; parent: number; group: number; session: number; startTicks: string };
let temporary: string | undefined;
let extraction: string | undefined;

function docker(args: string[], timeout = 15_000): string {
  const result = spawnSync("docker", args, { cwd: repoRoot, encoding: "utf8", timeout, stdio: "pipe" });
  assert.equal(result.status, 0, `owned Docker operation failed: ${result.error?.message || result.stderr.slice(-1000)}`);
  return result.stdout.trim();
}
const delay = async (milliseconds: number) => await new Promise((resolve) => setTimeout(resolve, milliseconds));

function identity(pid: number): (Identity & { state: string }) | null {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(") ") + 2).split(/\s+/u);
    return { pid, state: fields[0]!, parent: Number(fields[1]), group: Number(fields[2]),
      session: Number(fields[3]), startTicks: fields[19]! };
  } catch { return null; }
}

function alive(observed: Identity): boolean {
  const current = identity(observed.pid);
  return current !== null && current.startTicks === observed.startTicks && current.state !== "Z";
}

function gone(observed: Identity): boolean {
  const current = identity(observed.pid);
  return current === null || current.startTicks !== observed.startTicks;
}

function removeOwnedProcess(observed: Identity, executable: string): void {
  if (!alive(observed)) return;
  assert.equal(execFileSync("readlink", [`/proc/${observed.pid}/exe`], { encoding: "utf8" }).trim(), executable);
  process.kill(observed.pid, "SIGKILL");
}

async function port(): Promise<number> {
  const listener = net.createServer();
  await new Promise<void>((resolve) => listener.listen(0, "127.0.0.1", resolve));
  const address = listener.address();
  assert.ok(address && typeof address !== "string");
  const value = address.port;
  await new Promise<void>((resolve, reject) => listener.close((error) => error ? reject(error) : resolve()));
  return value;
}

async function waitJSON(file: string): Promise<Identity> {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try {
      const value = JSON.parse(readFileSync(file, "utf8")) as Identity;
      if (Number.isSafeInteger(value.pid) && value.pid > 0 && typeof value.startTicks === "string") return value;
    } catch { /* The fixture publishes a small process identity file. */ }
    await delay(30);
  }
  throw new Error("native lifecycle fixture did not publish its real process identity");
}

async function main(): Promise<void> {
  assert.ok(temporary);
  const compose = ["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml"];
  const original = docker([...compose, "ps", "-q", "mcp-instance-1"]);
  const redisOriginal = docker([...compose, "ps", "-q", "redis"]);
  assert.match(original, /^[a-f0-9]{64}$/u);
  assert.match(redisOriginal, /^[a-f0-9]{64}$/u);
  const image = docker(["inspect", "--format", "{{.Image}}", original]);
  const redisImage = docker(["inspect", "--format", "{{.Image}}", redisOriginal]);
  assert.equal(docker(["inspect", "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}', original]), sourceCommit);
  report.imageId = image;
  report.redisImageId = redisImage;
  extraction = docker(["create", image]);
  const runtime = path.join(temporary, "deepseek-stateless-mcp");
  const probe = path.join(temporary, "native-failover-probe");
  const hashes: Record<string, string> = {};
  for (const [name, destination] of [["deepseek-stateless-mcp", runtime], ["native-failover-probe", probe]]) {
    docker(["cp", `${extraction}:/usr/local/bin/${name}`, destination!]);
    chmodSync(destination!, 0o755);
    const actual = createHash("sha256").update(readFileSync(destination!)).digest("hex");
    assert.equal(docker(["exec", original, "sha256sum", `/usr/local/bin/${name}`]).split(/\s+/u)[0], actual);
    hashes[name!] = actual;
  }
  report.nativeBinarySha256 = hashes;
  docker(["rm", "--volumes", extraction]);
  extraction = undefined;

  for (const kind of ["timeout-regular", "timeout-detached", "parent-exit", "server-crash", "lease-loss"]) {
    const trace = path.join(temporary, kind);
    const workspace = path.join(trace, "workspace");
    mkdirSync(workspace, { recursive: true });
    writeFileSync(path.join(workspace, "target.txt"), "native lifecycle fixture\n");
    const row: Record<string, unknown> = { kind, caseStartedAt: new Date().toISOString() };
    rows.push(row);
    let redis: string | undefined;
    let service: ReturnType<typeof spawn> | undefined;
    let rootIdentity: Identity | undefined;
    let descendant: Identity | undefined;
    let descriptor: number | undefined;
    try {
      redis = docker(["run", "--detach", "--publish", "127.0.0.1::6379", "--label",
        `native-mcp-lifecycle-run=${process.env.GITHUB_RUN_ID}`, redisImage]);
      assert.match(redis, /^[a-f0-9]{64}$/u);
      const binding = docker(["port", redis, "6379/tcp"]);
      assert.match(binding, /^127\.0\.0\.1:\d+$/u);
      const redisDeadline = Date.now() + 10_000;
      let redisReady = false;
      do {
        const ping = spawnSync("docker", ["exec", redis, "redis-cli", "ping"], { encoding: "utf8", timeout: 1500 });
        redisReady = ping.status === 0 && ping.stdout.trim() === "PONG";
        if (!redisReady) await delay(50);
      } while (!redisReady && Date.now() < redisDeadline);
      assert.ok(redisReady, "owned disposable Redis did not become ready");
      const listen = await port();
      const token = process.env.MCP_AUTH_TOKEN || "";
      assert.ok(token);
      descriptor = openSync(path.join(output, `lifecycle-${kind}.log`), "w");
      service = spawn(runtime, [], { cwd: workspace, env: { ...process.env,
        MCP_HOST: "127.0.0.1", MCP_PORT: String(listen), MCP_WORKSPACE_ROOT: workspace,
        MCP_INSTANCE_ID: `lifecycle-${kind}-${process.env.GITHUB_RUN_ID}`,
        REDIS_URL: `redis://${binding}`, REDIS_PREFIX: `native-lifecycle:${process.env.GITHUB_RUN_ID}:${randomUUID()}`,
        MCP_TASK_PROGRAM: probe, MCP_TASK_POLL_MS: "50", MCP_TASK_LEASE_MS: "5000",
        MCP_TASK_TIMEOUT_SECONDS: "20", MCP_LIFECYCLE_TRACE_ROOT: trace,
      }, stdio: ["ignore", descriptor, descriptor] });
      let serviceFailure: string | undefined;
      service.on("error", (error) => { serviceFailure = error.message; });
      assert.ok(service.pid);
      row.servicePid = service.pid;
      const base = `http://127.0.0.1:${listen}`;
      const deadline = Date.now() + 10_000;
      let ready = false;
      while (!ready && Date.now() < deadline) {
        assert.equal(serviceFailure, undefined);
        assert.equal(service.exitCode, null);
        try { ready = (await fetch(`${base}/readyz`, { signal: AbortSignal.timeout(500) })).ok; } catch { /* startup */ }
        if (!ready) await delay(50);
      }
      assert.ok(ready, "actual native host service did not become ready through Redis");
      assert.equal(execFileSync("readlink", [`/proc/${service.pid}/exe`], { encoding: "utf8" }).trim(), runtime);
      async function call(name: string, arguments_: Record<string, unknown>): Promise<unknown> {
        const response = await fetch(`${base}/mcp`, { method: "POST", signal: AbortSignal.timeout(2000),
          headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: randomUUID(), method: "tools/call", params: { name, arguments: arguments_ } }),
        });
        assert.equal(response.status, 200);
        const body = await response.json() as { error?: unknown; result: { isError?: boolean; content: { text: string }[] } };
        assert.equal(body.error, undefined);
        assert.notEqual(body.result.isError, true);
        return JSON.parse(body.result.content[0]!.text);
      }
      const created = await call("start_test_run", { idempotencyKey: `lifecycle-${randomUUID()}`, target: "target.txt",
        keyword: `native-lifecycle-${kind}`, timeoutSeconds: kind.startsWith("timeout") ? 2 : 20 }) as { taskId: string };
      const observedAt = performance.now();
      rootIdentity = await waitJSON(path.join(trace, "root.json"));
      descendant = await waitJSON(path.join(trace, "descendant.json"));
      assert.ok(alive(rootIdentity) || kind === "parent-exit");
      assert.ok(alive(descendant));
      if (kind !== "timeout-regular") assert.equal(descendant.session, descendant.pid);
      row.rootIdentity = rootIdentity;
      row.descendantIdentity = descendant;
      row.taskBeforeInterruption = await call("get_task", { taskId: created.taskId });
      if (kind === "server-crash") {
        assert.ok(service.kill("SIGKILL"));
        row.actualServiceSIGKILL = true;
      }
      if (kind === "lease-loss") {
        docker(["stop", "--time", "1", redis]);
        row.actualRedisStopped = true;
      }
      const deadlineSample = observedAt + 3000;
      while (performance.now() < deadlineSample) await delay(30);
      row.rootAliveAtDeadline = alive(rootIdentity);
      row.descendantAliveAtDeadline = alive(descendant);
      row.rootReapedAtDeadline = gone(rootIdentity);
      row.descendantReapedAtDeadline = gone(descendant);
      if (!["server-crash", "lease-loss"].includes(kind)) row.taskAtDeadline = await call("get_task", { taskId: created.taskId });
      if (kind === "lease-loss") row.backendNotReady = !(await fetch(`${base}/readyz`, { signal: AbortSignal.timeout(1500) })).ok;
      const observationEnd = observedAt + 6500;
      while (performance.now() < observationEnd) await delay(30);
      row.lateEffectPresent = existsSync(path.join(trace, "late-effect.txt"));
      if (row.lateEffectPresent) row.lateEffectContents = readFileSync(path.join(trace, "late-effect.txt"), "utf8");
      if (!["server-crash", "lease-loss"].includes(kind)) {
        const task = await call("get_task", { taskId: created.taskId }) as TaskRecord;
        assert.equal(task.attempts, 1);
        row.taskAfterObservation = task;
      }
      row.rootAliveBeforeManualCleanup = alive(rootIdentity);
      row.descendantAliveBeforeManualCleanup = alive(descendant);
      const terminal = row.taskAtDeadline as TaskRecord | undefined;
      row.taskSettledWithinDeadline = terminal === undefined ||
        (kind === "parent-exit" ? terminal.status === "succeeded" : terminal.status === "failed");
      row.noLateEffectAndDescendantsStopped = row.lateEffectPresent === false &&
        row.rootReapedAtDeadline === true && row.descendantReapedAtDeadline === true && row.taskSettledWithinDeadline === true;
    } finally {
      // These identities and executable paths belong only to the synthetic cloud fixture.
      if (rootIdentity) removeOwnedProcess(rootIdentity, probe);
      if (descendant) removeOwnedProcess(descendant, probe);
      if (service?.pid && service.exitCode === null && service.signalCode === null) service.kill("SIGKILL");
      if (service?.pid) await new Promise<void>((resolve) => {
        if (service!.exitCode !== null || service!.signalCode !== null) resolve();
        else {
          const timer = setTimeout(resolve, 3000);
          service!.once("exit", () => { clearTimeout(timer); resolve(); });
        }
      });
      if (descriptor !== undefined) closeSync(descriptor);
      if (redis) docker(["rm", "--force", "--volumes", redis]);
    }
  }
  assert.ok(rows.every((row) => row.noLateEffectAndDescendantsStopped === true),
    "native task descendants remained active or produced a late effect after execution authority ended");
  report.status = "PASS";
  report.nativeDescendantLifecycleQualified = true;
}

mkdirSync(output, { recursive: true });
temporary = mkdtempSync(path.join(os.tmpdir(), "native-mcp-lifecycle-"));
try {
  await main();
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  throw error;
} finally {
  writeFileSync(path.join(output, "process-lifecycle.json"), `${JSON.stringify(report, null, 2)}\n`);
  if (extraction) docker(["rm", "--volumes", extraction]);
  assert.equal(path.dirname(temporary), path.resolve(os.tmpdir()));
  assert.ok(path.basename(temporary).startsWith("native-mcp-lifecycle-"));
  rmSync(temporary, { recursive: true, force: true });
}
