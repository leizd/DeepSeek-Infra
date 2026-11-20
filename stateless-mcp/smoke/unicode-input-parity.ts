// Offline original service versus actual stock native image. Cloud-only synthetic Unicode input data.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { chmodSync, closeSync, mkdtempSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { parseToolText, RetryingMcpClient } from "../src/retrying-client.js";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") throw new Error("native Unicode input qualification is cloud Linux only");
const repo = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repo, "artifacts/native-stateless-mcp");
const sha = process.env.GITHUB_SHA!;
assert.match(sha, /^[a-f0-9]{40}$/u);
const run = process.env.GITHUB_RUN_ID!;
const token = process.env.MCP_AUTH_TOKEN!;
assert.ok(token);
const report: Record<string, unknown> = { status: "FAIL", sourceCommit: sha, runId: run,
  event: process.env.GITHUB_EVENT_NAME, scope: "actual public MCP Unicode input acceptance and durable admission only",
  taskEngineSuccessQualified: false, noProductionSourceChanged: true,
  controlledReadOnlyHostMounts: 1, usesCloudHostNetwork: true, fullMigrationQualified: false, fullSandboxQualified: false };
interface SearchResult { matches: string[]; truncated: boolean }
interface Service { name: string; instanceId: string; base: string; pid: number; container?: string; child?: ReturnType<typeof spawn> }
const services: Service[] = [];
let redis: string | undefined;
let extraction: string | undefined;
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
interface InputCase { name: string; tool: string; arguments: Record<string, unknown>; accepted: boolean }
const cases: InputCase[] = [
  { name: "search-ascii-control", tool: "code_search", arguments: { query: "needle", path: "control.txt" }, accepted: true },
  ...[
    ["chinese-300", "汉".repeat(300), true],
    ["chinese-500", "汉".repeat(500), true],
    ["emoji-250", "🔬".repeat(250), true],
    ["emoji-500", "🔬".repeat(500), true],
    ["emoji-501", "🔬".repeat(501), false],
    ["ascii-500", "x".repeat(500), true],
    ["ascii-501", "x".repeat(501), false],
    ["empty", "", false],
  ].map(([name, query, accepted]) => ({ name: `search-${name}`, tool: "code_search", arguments: { query: String(query), path: "control.txt" }, accepted: accepted === true })),
  ...[
    ["ascii-8", "abcdefgh", true],
    ["emoji-2", "🔬".repeat(2), false],
    ["emoji-8", "🔬".repeat(8), true],
    ["emoji-200", "🔬".repeat(200), true],
    ["emoji-201", "🔬".repeat(201), false],
  ].map(([name, idempotencyKey, accepted]) => ({ name: `key-${name}`, tool: "start_test_run", arguments: { idempotencyKey: String(idempotencyKey), target: "test_unicode_input_fixture.py::test_retained_input", timeoutSeconds: 1 }, accepted: accepted === true })),
];
async function measureInputs(service: Service, rows: Record<string, unknown>[]): Promise<void> {
  const observations: Record<string, unknown>[] = [];
  rows.push({ name: service.name, pid: service.pid, instanceId: service.instanceId, cases: observations });
  for (const input of cases) {
    const client = new RetryingMcpClient([`${service.base}/mcp`], token, 1);
    let response: unknown;
    let accepted = false;
    let responseKind = "tool-result";
    try {
      const result = await client.callTool(input.tool, input.arguments);
      assert.equal(result.attempts, 1);
      response = result.result;
      accepted = result.result.isError !== true;
      if (accepted) {
        const value = parseToolText<Record<string, unknown>>(result.result);
        if (input.tool === "code_search") {
          assert.deepEqual(value, { matches: input.name === "search-ascii-control" ? ["1:1:needle control"] : [], truncated: false });
        } else {
          assert.equal(typeof value.taskId, "string");
          assert.match(String(value.taskId), /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu);
          // Read the actual durable admission back; this is not test-engine success.
          const stored = parseToolText<Record<string, unknown>>((await client.callTool("get_task", { taskId: value.taskId })).result);
          assert.equal(stored.id, value.taskId);
          response = { admission: result.result, retainedTask: stored };
        }
      }
    } catch (error) {
      if (!(error instanceof Error) || !("code" in error) || error.code !== -32602) throw error;
      responseKind = "rpc-invalid-params";
      response = { code: error.code, message: error.message };
    }
    const text = String(input.arguments.query ?? input.arguments.idempotencyKey);
    observations.push({ name: input.name, tool: input.tool, expectedAccepted: input.accepted, accepted, responseKind,
      codePoints: Array.from(text).length, utf16Units: text.length, utf8Bytes: Buffer.byteLength(text), response });
  }
  if (service.name === "original-typescript") {
    for (const row of observations) assert.equal(row.accepted, row.expectedAccepted, `original public schema control failed: ${String(row.name)}`);
  }
}
async function main(): Promise<void> {
  mkdirSync(output, { recursive: true });
  const temporary = mkdtempSync(path.join(os.tmpdir(), "native-mcp-unicode-"));
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
  // Both implementations use the actual production-image rg executable. The
  // original production Node image also used bookworm, not the runner's rg.
  const tools = path.join(temporary, "production-tools"); mkdirSync(tools);
  const sharedRg = path.join(tools, "rg");
  extraction = docker(["create", image]);
  docker(["cp", `${extraction}:/usr/bin/rg`, sharedRg]); chmodSync(sharedRg, 0o755);
  docker(["rm", "--volumes", extraction]); extraction = undefined;
  const toolHash = createHash("sha256").update(readFileSync(sharedRg)).digest("hex");
  assert.equal(toolHash, docker(["run", "--rm", "--network", "none", "--entrypoint", "sha256sum", image, "/usr/bin/rg"]).split(/\s+/u)[0]);
  const toolVersion = spawnSync(sharedRg, ["--version"], { encoding: "utf8", timeout: 5000 });
  assert.equal(toolVersion.status, 0, toolVersion.stderr);
  report.sharedProductionRipgrepSha256 = toolHash;
  report.sharedProductionRipgrepVersion = toolVersion.stdout.trim();
  report.originalUsesActualProductionRipgrep = true;
  report.originalServerSourceSha256 = createHash("sha256").update(readFileSync(path.join(repo, "stateless-mcp/src/server.ts"))).digest("hex");
  report.originalSearchSourceSha256 = createHash("sha256").update(readFileSync(path.join(repo, "stateless-mcp/src/code-search.ts"))).digest("hex");
  report.originalInputSchemaSourceSha256 = createHash("sha256").update(readFileSync(path.join(repo, "stateless-mcp/src/mcp-server.ts"))).digest("hex");
  const existingRedis = docker(["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml", "ps", "-q", "redis"]);
  const redisImage = docker(["inspect", "--format", "{{.Image}}", existingRedis]);
  redis = docker(["run", "--detach", "--publish", "127.0.0.1::6379", "--label", `native-mcp-unicode-run=${run}`, redisImage]);
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
    writeFileSync(path.join(workspace, "test_unicode_input_fixture.py"), "def test_retained_input():\n    assert True\n");
    const fixtureHash = createHash("sha256").update(readFileSync(path.join(workspace, "test_unicode_input_fixture.py"))).digest("hex");
    if (report.fixtureSha256 === undefined) report.fixtureSha256 = fixtureHash;
    assert.equal(fixtureHash, report.fixtureSha256);
    const listen = await port();
    const environment = { ...process.env, REDIS_URL: `redis://${redisAddress}`, REDIS_PREFIX: `native-search:${run}:${randomUUID()}`,
      MCP_HOST: "127.0.0.1", MCP_PORT: String(listen), MCP_WORKSPACE_ROOT: workspace, MCP_INSTANCE_ID: `search-${name}-${run}`,
      MCP_MAX_OUTPUT_BYTES: "4096", TOKIO_WORKER_THREADS: "1", PATH: tools + path.delimiter + (process.env.PATH || "") };
    let service: Service;
    if (name === "original-typescript") {
      descriptor = openSync(path.join(output, "unicode-original-service.log"), "w");
      const child = spawn(process.execPath, [path.join(repo, "stateless-mcp/dist/src/server.js")], { cwd: workspace, env: environment, stdio: ["ignore", descriptor, descriptor] });
      assert.ok(child.pid); closeSync(descriptor); descriptor = undefined;
      service = { name, instanceId: environment.MCP_INSTANCE_ID, child, pid: child.pid, base: `http://127.0.0.1:${listen}` };
    } else {
      const container = docker(["run", "--detach", "--network", "host", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "512m", "--pids-limit", "64",
        "--label", `native-mcp-unicode-run=${run}`, "--mount", `type=bind,source=${workspace},target=/workspace,readonly`,
        "-e", `REDIS_URL=${environment.REDIS_URL}`, "-e", `REDIS_PREFIX=${environment.REDIS_PREFIX}`, "-e", `MCP_AUTH_TOKEN=${token}`,
        "-e", "MCP_HOST=127.0.0.1", "-e", `MCP_PORT=${listen}`, "-e", "MCP_WORKSPACE_ROOT=/workspace", "-e", "MCP_MAX_OUTPUT_BYTES=4096",
        "-e", "TOKIO_WORKER_THREADS=1", "-e", `MCP_INSTANCE_ID=${environment.MCP_INSTANCE_ID}`, image]);
      assert.match(container, /^[a-f0-9]{64}$/u);
      const pid = Number(docker(["inspect", "--format", "{{.State.Pid}}", container])); assert.ok(Number.isSafeInteger(pid) && pid > 0);
      service = { name, instanceId: environment.MCP_INSTANCE_ID, container, pid, base: `http://127.0.0.1:${listen}` };
    }
    services.push(service); await ready(service);
    await measureInputs(service, rows);
  }
  const native = services.find((service) => service.name === "native-rust")!;
  assert.equal(docker(["inspect", "--format", "{{.State.OOMKilled}}", native.container!]), "false");
  report.actualOOM = false;
  const originalCases = rows[0]!.cases as Record<string, unknown>[];
  const nativeCases = rows[1]!.cases as Record<string, unknown>[];
  assert.equal(originalCases.length, cases.length); assert.equal(nativeCases.length, cases.length);
  const mismatches = nativeCases.filter((row) => row.accepted !== row.expectedAccepted).map((row) => row.name);
  report.actualCasesPerService = cases.length;
  report.counterexamples = mismatches;
  assert.deepEqual(mismatches, [], "native public Unicode input acceptance differs from the retained original service");
  report.status = "PASS";
}
try { await main(); }
catch (error) { report.error = error instanceof Error ? error.message : String(error); process.exitCode = 1; }
finally {
  // Retain actual observations before any cleanup can fail.
  writeFileSync(path.join(output, "unicode-input-parity.json"), `${JSON.stringify(report, null, 2)}\n`);
  const cleanupErrors: string[] = [];
  for (const service of services) {
    try {
    if (service.container !== undefined) {
      writeFileSync(path.join(output, "unicode-native-service.log"), docker(["logs", service.container]));
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
  if (extraction !== undefined) {
    try { docker(["rm", "--force", "--volumes", extraction]); }
    catch (error) { cleanupErrors.push(error instanceof Error ? error.message : String(error)); }
  }
  if (descriptor !== undefined) closeSync(descriptor);
  writeFileSync(path.join(output, "unicode-input-parity.json"), `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ status: report.status, sourceCommit: sha, error: report.error, counterexamples: report.counterexamples }));
}
