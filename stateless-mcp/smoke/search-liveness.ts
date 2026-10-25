// Offline original service versus actual stock native image. Cloud-only synthetic data.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { chmodSync, closeSync, constants, mkdtempSync, mkdirSync, openSync, readFileSync, writeFileSync, writeSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { parseToolText, RetryingMcpClient } from "../src/retrying-client.js";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") throw new Error("native search qualification is cloud Linux only");
const repo = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repo, "artifacts/native-stateless-mcp");
const sha = process.env.GITHUB_SHA!;
assert.match(sha, /^[a-f0-9]{40}$/u);
const run = process.env.GITHUB_RUN_ID!;
const token = process.env.MCP_AUTH_TOKEN!;
assert.ok(token);
const report: Record<string, unknown> = { status: "FAIL", sourceCommit: sha, runId: run,
  event: process.env.GITHUB_EVENT_NAME, scope: "Linux actual code_search concurrency/output custody only",
  healthDeadlineMs: 500, maxOutputBytes: 4096, memoryGrowthBoundKiB: 32768,
  controlledReadOnlyHostMounts: 1, usesCloudHostNetwork: true, fullMigrationQualified: false, fullSandboxQualified: false };
interface SearchResult { matches: string[]; truncated: boolean }
interface Service { name: string; instanceId: string; base: string; hwm: () => number; pid: number; container?: string; child?: ReturnType<typeof spawn> }
const services: Service[] = [];
let redis: string | undefined;
let descriptor: number | undefined;
const delay = async (ms: number) => await new Promise<void>((resolve) => setTimeout(resolve, ms));
function docker(args: string[]): string {
  const result = spawnSync("docker", args, { cwd: repo, encoding: "utf8", timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
  assert.equal(result.status, 0, result.error?.message || result.stderr.slice(-2000));
  return result.stdout.trim();
}
async function port(): Promise<number> {
  const server = net.createServer();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address(); assert.ok(address && typeof address !== "string");
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return address.port;
}
function peak(status: string): number {
  const value = /^VmHWM:\s+(\d+) kB$/mu.exec(status); assert.ok(value, "missing actual kernel resident high-water mark");
  return Number(value[1]);
}
async function ready(service: Service): Promise<void> {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`${service.base}/readyz`, { signal: AbortSignal.timeout(500) })).ok) {
        const response = await fetch(`${service.base}/instance`, { signal: AbortSignal.timeout(500) });
        assert.ok(response.ok);
        assert.equal(((await response.json()) as { instanceId: string }).instanceId, service.instanceId);
        return;
      }
    } catch { /* startup; a response from another process is not a ready control */ }
    await delay(50);
  }
  throw new Error(`${service.name} did not become ready through real Redis`);
}
async function call(service: Service, target: string): Promise<SearchResult> {
  const client = new RetryingMcpClient([`${service.base}/mcp`], token, 1);
  return parseToolText<SearchResult>((await client.callTool("code_search", { query: "needle", path: target, maxResults: 200 })).result);
}
function isLive(service: Service): boolean {
  if (service.container !== undefined) return docker(["inspect", "--format", "{{.State.Running}}", service.container]) === "true";
  return service.child?.exitCode === null && service.child?.signalCode === null;
}
async function measure(service: Service, workspace: string, rows: Record<string, unknown>[]): Promise<Record<string, unknown>> {
  const control = await call(service, "control.txt");
  assert.deepEqual(control, { matches: ["1:1:needle control"], truncated: false });
  const fifo = path.join(workspace, "slow.pipe");
  const made = spawnSync("mkfifo", ["--mode=666", fifo], { encoding: "utf8", timeout: 2000 }); assert.equal(made.status, 0, made.stderr);
  let settled = false;
  let pendingError: unknown;
  const search = call(service, "slow.pipe").then((value) => { settled = true; return value; }, (error: unknown) => { settled = true; pendingError = error; return null; });
  let writer: number | undefined;
  const observation: Record<string, unknown> = { name: service.name, pid: service.pid, controlPassed: true };
  rows.push(observation);
  try {
    const deadline = Date.now() + 2000;
    while (writer === undefined && Date.now() < deadline && !settled) {
      try { writer = openSync(fifo, constants.O_WRONLY | constants.O_NONBLOCK); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENXIO") throw error; await delay(10); }
    }
    assert.notEqual(writer, undefined, `actual rg FIFO reader not reached: ${String(pendingError)}`);
    writeSync(writer!, Buffer.from("needle controlled pipe\n"));
    assert.equal(settled, false, "search already completed before the held-open pipe observation");
    const start = performance.now();
    let healthResponded = false;
    try { healthResponded = (await fetch(`${service.base}/healthz`, { signal: AbortSignal.timeout(500) })).ok; } catch { /* Record actual timeout before releasing FIFO. */ }
    observation.healthElapsedMs = performance.now() - start;
    observation.healthRespondedDuringHeldOpenSearch = healthResponded;
    observation.searchStillInFlightAtHealthObservation = !settled;
    observation.serviceAliveAtHealthObservation = isLive(service);
  } finally { if (writer !== undefined) closeSync(writer); }
  const pipeResult = await search; assert.equal(pendingError, undefined); assert.notEqual(pipeResult, null);
  const visiblePipe = service.name === "native-rust" ? "/workspace/slow.pipe" : fifo;
  observation.visiblePipe = visiblePipe;
  assert.deepEqual(pipeResult, { matches: [`${visiblePipe}:1:1:needle controlled pipe`], truncated: false });
  observation.realPipeSearchResult = pipeResult;
  const before = service.hwm();
  const large = await call(service, "large.txt");
  const after = service.hwm();
  observation.largeSearchResult = large;
  observation.kernelVmHwmBeforeKiB = before; observation.kernelVmHwmAfterKiB = after;
  observation.kernelVmHwmGrowthKiB = Math.max(0, after - before);
  assert.equal(large.truncated, true); assert.equal(large.matches.length, 1);
  assert.equal(large.matches[0], "1:1:needle" + "x".repeat(4096 - 10));
  observation.actualCappedBytes = Buffer.byteLength(large.matches.join("\n"));
  observation.kernelVmHwmBeforeKiB = before; observation.kernelVmHwmAfterKiB = after;
  observation.kernelVmHwmGrowthKiB = Math.max(0, after - before);
  observation.memoryGrowthWithinBound = Math.max(0, after - before) <= 32768;
  observation.serviceAliveAfterLargeSearch = isLive(service);
  return observation;
}
async function main(): Promise<void> {
  mkdirSync(output, { recursive: true });
  const temporary = mkdtempSync(path.join(os.tmpdir(), "native-mcp-search-"));
  const image = `native-mcp-durability:${run}-${sha.slice(0, 12)}`;
  const metadata = JSON.parse(docker(["image", "inspect", image]))[0];
  assert.equal(metadata.Config.Labels["org.opencontainers.image.revision"], sha);
  report.imageId = metadata.Id;
  report.nativeBinarySha256 = docker(["run", "--rm", "--network", "none", "--entrypoint", "sha256sum", image, "/usr/local/bin/deepseek-stateless-mcp"]).split(/\s+/u)[0];
  const files = docker(["run", "--rm", "--network", "none", "--entrypoint", "ls", image, "/usr/local/bin"]).split(/\r?\n/u);
  assert.deepEqual(files, ["deepseek-stateless-mcp"]); report.actualProductionLocalExecutables = files;
  const foreign = spawnSync("docker", ["run", "--rm", "--network", "none", "--entrypoint", "sh", image,
    "-c", 'for name in python python3 node; do if command -v "$name"; then exit 42; fi; done; exit 0'], { encoding: "utf8", timeout: 5000 });
  report.actualInterpreterProbeExitCode = foreign.status;
  report.interpreterProbeStdout = foreign.stdout.slice(0, 1024);
  report.interpreterProbeStderr = foreign.stderr.slice(0, 1024);
  assert.equal(foreign.status, 0, "stock image interpreter absence probe did not complete");
  report.productionImageContainsNodeOrPython = false;
  report.originalServerSourceSha256 = createHash("sha256").update(readFileSync(path.join(repo, "stateless-mcp/src/server.ts"))).digest("hex");
  report.originalSearchSourceSha256 = createHash("sha256").update(readFileSync(path.join(repo, "stateless-mcp/src/code-search.ts"))).digest("hex");
  const existingRedis = docker(["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml", "ps", "-q", "redis"]);
  const redisImage = docker(["inspect", "--format", "{{.Image}}", existingRedis]);
  redis = docker(["run", "--detach", "--publish", "127.0.0.1::6379", "--label", `native-mcp-search-run=${run}`, redisImage]);
  assert.match(redis, /^[a-f0-9]{64}$/u);
  const redisAddress = docker(["port", redis, "6379/tcp"]); assert.match(redisAddress, /^127\.0\.0\.1:\d+$/u);
  const redisDeadline = Date.now() + 10000;
  let redisReady = false;
  while (!redisReady && Date.now() < redisDeadline) {
    const ping = spawnSync("docker", ["exec", redis, "redis-cli", "ping"], { encoding: "utf8", timeout: 1500 });
    redisReady = ping.status === 0 && ping.stdout.trim() === "PONG";
    if (!redisReady) await delay(50);
  }
  assert.ok(redisReady, "owned disposable Redis was not ready");
  const rows: Record<string, unknown>[] = []; report.observations = rows;
  for (const name of ["original-typescript", "native-rust"]) {
    const workspace = path.join(temporary, name); mkdirSync(workspace); chmodSync(workspace, 0o777);
    writeFileSync(path.join(workspace, "control.txt"), "needle control\n");
    const data = Buffer.alloc(2 * 1024 * 1024, "x"); data.write("needle"); data[data.length - 1] = 10;
    const large = openSync(path.join(workspace, "large.txt"), "wx", 0o644);
    const corpus = createHash("sha256");
    try { for (let i = 0; i < 32; i += 1) { writeSync(large, data); corpus.update(data); } } finally { closeSync(large); }
    const corpusSha256 = corpus.digest("hex");
    if (report.corpusSha256 === undefined) report.corpusSha256 = corpusSha256;
    assert.equal(corpusSha256, report.corpusSha256);
    report.corpusBytes = data.length * 32;
    const listen = await port();
    const environment = { ...process.env, REDIS_URL: `redis://${redisAddress}`, REDIS_PREFIX: `native-search:${run}:${randomUUID()}`,
      MCP_HOST: "127.0.0.1", MCP_PORT: String(listen), MCP_WORKSPACE_ROOT: workspace, MCP_INSTANCE_ID: `search-${name}-${run}`,
      MCP_MAX_OUTPUT_BYTES: "4096", TOKIO_WORKER_THREADS: "1" };
    let service: Service;
    if (name === "original-typescript") {
      descriptor = openSync(path.join(output, "search-original-service.log"), "w");
      const child = spawn(process.execPath, [path.join(repo, "stateless-mcp/dist/src/server.js")], { cwd: workspace, env: environment, stdio: ["ignore", descriptor, descriptor] });
      assert.ok(child.pid); closeSync(descriptor); descriptor = undefined;
      service = { name, instanceId: environment.MCP_INSTANCE_ID, child, pid: child.pid, base: `http://127.0.0.1:${listen}`, hwm: () => peak(readFileSync(`/proc/${child.pid}/status`, "utf8")) };
    } else {
      const container = docker(["run", "--detach", "--network", "host", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "512m", "--pids-limit", "64",
        "--label", `native-mcp-search-run=${run}`, "--mount", `type=bind,source=${workspace},target=/workspace,readonly`,
        "-e", `REDIS_URL=${environment.REDIS_URL}`, "-e", `REDIS_PREFIX=${environment.REDIS_PREFIX}`, "-e", `MCP_AUTH_TOKEN=${token}`,
        "-e", "MCP_HOST=127.0.0.1", "-e", `MCP_PORT=${listen}`, "-e", "MCP_WORKSPACE_ROOT=/workspace", "-e", "MCP_MAX_OUTPUT_BYTES=4096",
        "-e", "TOKIO_WORKER_THREADS=1", "-e", `MCP_INSTANCE_ID=${environment.MCP_INSTANCE_ID}`, image]);
      assert.match(container, /^[a-f0-9]{64}$/u);
      const pid = Number(docker(["inspect", "--format", "{{.State.Pid}}", container])); assert.ok(Number.isSafeInteger(pid) && pid > 0);
      service = { name, instanceId: environment.MCP_INSTANCE_ID, container, pid, base: `http://127.0.0.1:${listen}`, hwm: () => peak(docker(["exec", container, "cat", "/proc/1/status"])) };
    }
    services.push(service); await ready(service);
    await measure(service, workspace, rows);
    if (name === "original-typescript") {
      assert.equal(rows[0]!.healthRespondedDuringHeldOpenSearch, true, "original HTTP liveness control failed");
      // Keep the reference's real V8 process peak visible. The physical32MiB
      // budget qualifies the native implementation; legacy GC is observational.
      report.originalMemoryGrowthWithinNativeBudget = rows[0]!.memoryGrowthWithinBound;
      report.originalMemoryIsObservedOnly = true;
    }
  }
  const native = services.find((s) => s.name === "native-rust")!;
  assert.equal(docker(["inspect", "--format", "{{.State.OOMKilled}}", native.container!]), "false");
  report.actualOOM = false;
  assert.equal(rows[1]!.healthRespondedDuringHeldOpenSearch, true, "native HTTP worker was blocked by actual code_search");
  assert.equal(rows[1]!.memoryGrowthWithinBound, true, "native search collected output beyond the configured cap before truncation");
  report.status = "PASS";
}
try { await main(); }
catch (error) { report.error = error instanceof Error ? error.message : String(error); process.exitCode = 1; }
finally {
  // Retain actual observations before any cleanup can fail.
  writeFileSync(path.join(output, "search-liveness.json"), `${JSON.stringify(report, null, 2)}\n`);
  const cleanupErrors: string[] = [];
  for (const service of services) {
    try {
    if (service.container !== undefined) {
      writeFileSync(path.join(output, "search-native-service.log"), docker(["logs", service.container]));
      docker(["rm", "--force", "--volumes", service.container]);
    } else if (service.child !== undefined && service.child.exitCode === null && service.child.signalCode === null) {
      service.child.kill("SIGTERM");
      const deadline = Date.now() + 3000;
      while (service.child.exitCode === null && service.child.signalCode === null && Date.now() < deadline) await delay(25);
      if (service.child.exitCode === null && service.child.signalCode === null) service.child.kill("SIGKILL");
    }
    } catch (error) { cleanupErrors.push(error instanceof Error ? error.message : String(error)); }
  }
  if (redis !== undefined) {
    try { docker(["rm", "--force", "--volumes", redis]); }
    catch (error) { cleanupErrors.push(error instanceof Error ? error.message : String(error)); }
  }
  report.cleanupErrors = cleanupErrors;
  if (cleanupErrors.length) { report.status = "FAIL"; process.exitCode = 1; }
  if (descriptor !== undefined) closeSync(descriptor);
  writeFileSync(path.join(output, "search-liveness.json"), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, sourceCommit: sha, error: report.error, observations: report.observations }));
}
