import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

if (process.env.GITHUB_ACTIONS !== "true" || process.platform !== "linux") {
  throw new Error("native MCP production-image qualification is cloud-only");
}
const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const output = path.join(repoRoot, "artifacts", "native-stateless-mcp");
const git = (...args: string[]) => execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
const sourceCommit = git("rev-parse", "HEAD");
const sourceTree = git("rev-parse", "HEAD^{tree}");
assert.equal(sourceCommit, process.env.GITHUB_SHA);
assert.equal(git("status", "--porcelain", "--untracked-files=all"), "");
assert.match(process.env.GITHUB_RUN_ID || "", /^\d+$/u);
const image = `native-mcp-durability:${process.env.GITHUB_RUN_ID}-${sourceCommit.slice(0, 12)}`;
const report: Record<string, unknown> = {
  sourceCommit, sourceTree, runId: process.env.GITHUB_RUN_ID, event: process.env.GITHUB_EVENT_NAME,
  checkedAt: new Date().toISOString(), status: "FAIL", readiness: "NOT_READY",
  localNativeCompilation: false, fullMigrationQualified: false,
  productionTarget: "production", testFixtureInProductionImage: false,
};

function docker(args: string[], timeout = 15_000): string {
  const result = spawnSync("docker", args, { cwd: repoRoot, encoding: "utf8", timeout, stdio: "pipe" });
  assert.equal(result.status, 0, `owned Docker operation failed: ${result.error?.message || result.stderr.slice(-1200)}`);
  return result.stdout.trim();
}
const delay = async (milliseconds: number) => await new Promise((resolve) => setTimeout(resolve, milliseconds));
const cases: Record<string, unknown>[] = [];
report.cases = cases;

async function verifyInstanceIdentity(): Promise<void> {
  const compose = ["compose", "-f", "docker-compose.stateless-mcp.yml", "-f", "stateless-mcp/compose.failover.yml"];
  const redis = docker([...compose, "ps", "-q", "redis"]);
  assert.match(redis, /^[a-f0-9]{64}$/u);
  const redisState = JSON.parse(docker(["inspect", redis]))[0];
  const networks = Object.keys(redisState.NetworkSettings.Networks);
  assert.deepEqual(networks, [`${process.env.COMPOSE_PROJECT_NAME}_default`]);
  const network = networks[0]!;
  const observations: Record<string, unknown>[] = [];
  const identity: Record<string, unknown> = { network, cases: observations };
  report.instanceIdentity = identity;

  async function info(container: string): Promise<{ instanceId: string; hostPid: number; containerPid: number }> {
    const deadline = Date.now() + 10_000;
    let ready = false;
    do {
      const state = JSON.parse(docker(["inspect", container]))[0];
      assert.equal(state.State.Running, true, "owned native instance stopped before observation");
      const result = spawnSync("docker", ["exec", container, "curl", "--fail", "--silent", "--show-error", "--max-time", "1",
        "http://127.0.0.1:8010/readyz"], { cwd: repoRoot, encoding: "utf8", timeout: 3000 });
      ready = result.status === 0;
      if (!ready) await delay(150);
    } while (!ready && Date.now() < deadline);
    assert.ok(ready, "owned native instance did not become ready through Redis");
    const state = JSON.parse(docker(["inspect", container]))[0];
    assert.equal(state.HostConfig.NetworkMode, network);
    assert.ok(["", "private"].includes(state.HostConfig.PidMode));
    assert.deepEqual(state.Config.Cmd, ["deepseek-stateless-mcp"]);
    assert.equal(state.Config.Labels["org.opencontainers.image.revision"], sourceCommit);
    assert.deepEqual(state.Mounts, []);
    assert.ok(!state.HostConfig.PortBindings || Object.keys(state.HostConfig.PortBindings).length === 0);
    const roles = docker(["top", container, "-eo", "pid,args"]).split(/\r?\n/u).slice(1)
      .map((row) => path.basename(row.trim().split(/\s+/u)[1] || ""));
    assert.deepEqual(roles, ["deepseek-stateless-mcp"]);
    assert.match(docker(["exec", container, "cat", "/proc/1/stat"]), /^1 /u);
    const rpc = JSON.parse(docker(["exec", container, "curl", "--fail", "--silent", "--show-error", "--max-time", "2",
      "-H", "content-type: application/json", "--data-binary",
      JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "server_info", arguments: {} } }),
      "http://127.0.0.1:8010/mcp"]));
    const value = JSON.parse(rpc.result.content[0].text);
    assert.equal(value.durableTaskState, "redis");
    assert.equal(typeof value.instanceId, "string");
    assert.ok(Number.isSafeInteger(state.State.Pid) && state.State.Pid > 0);
    return { instanceId: value.instanceId, hostPid: state.State.Pid, containerPid: 1 };
  }

  for (const [name, environment] of [["unset", []], ["empty", ["-e", "MCP_INSTANCE_ID="]],
    ["whitespace", ["-e", "MCP_INSTANCE_ID=   "]], ["explicit", ["-e", "MCP_INSTANCE_ID=operator-identity"]]] as const) {
    const owned: string[] = [];
    const row: Record<string, unknown> = { name, mounts: 0, publishedPorts: 0 };
    observations.push(row);
    try {
      const start = () => {
        const id = docker(["run", "--detach", "--network", network, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
          "--memory", "256m", "--pids-limit", "64", "--label", `native-mcp-identity-run=${process.env.GITHUB_RUN_ID}`,
          "-e", "REDIS_URL=redis://redis:6379", "-e", `REDIS_PREFIX=native-identity-${process.env.GITHUB_RUN_ID}-${name}`,
          ...environment, image]);
        assert.match(id, /^[a-f0-9]{64}$/u);
        owned.push(id);
        return id;
      };
      const firstContainer = start();
      const first = await info(firstContainer);
      row.first = first;
      if (name !== "explicit") {
        const second = await info(start());
        assert.notEqual(first.hostPid, second.hostPid);
        row.second = second;
      }
      docker(["restart", "--timeout", "3", firstContainer]);
      const restarted = await info(firstContainer);
      assert.notEqual(first.hostPid, restarted.hostPid);
      row.restarted = restarted;
      if (name === "explicit") {
        assert.equal(first.instanceId, "operator-identity");
        assert.equal(restarted.instanceId, first.instanceId);
        row.explicitIdentityRetained = true;
      } else {
        const second = row.second as { instanceId: string };
        const ids = [first.instanceId, second.instanceId, restarted.instanceId];
        row.defaultIdentityUnique = new Set(ids).size === 3 && ids.every((id) => /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/u.test(id));
      }
    } finally {
      for (const container of owned) docker(["rm", "--force", container]);
    }
  }
  assert.ok(observations.filter((row) => row.name !== "explicit").every((row) => row.defaultIdentityUnique === true),
    "native production instances share a default owner identity");
  identity.defaultIdentityQualified = true;
}

async function main(): Promise<void> {
  const build = spawnSync("docker", ["build", "--target", "production", "--build-arg",
    `NATIVE_SOURCE_REVISION=${sourceCommit}`, "-f", "stateless-mcp/Dockerfile", "-t", image, "."],
  { cwd: repoRoot, encoding: "utf8", timeout: 600_000, maxBuffer: 16 * 1024 * 1024 });
  writeFileSync(path.join(output, "default-durability-build.log"), build.stdout + build.stderr);
  assert.equal(build.status, 0, `production image build failed: ${build.error?.message || build.stderr.slice(-1200)}`);
  const metadata = JSON.parse(docker(["image", "inspect", image]))[0];
  assert.equal(metadata.Config.Labels["org.opencontainers.image.revision"], sourceCommit);
  assert.deepEqual(metadata.Config.Cmd, ["deepseek-stateless-mcp"]);
  assert.ok(!metadata.Config.Env.some((value: string) => value.startsWith("REDIS_URL=") || value.startsWith("MCP_TASK_PROGRAM=")));
  assert.match(metadata.Id, /^sha256:[a-f0-9]{64}$/u);
  report.imageId = metadata.Id;
  report.revisionLabel = metadata.Config.Labels["org.opencontainers.image.revision"];
  const files = docker(["run", "--rm", "--network", "none", "--entrypoint", "ls", image, "/usr/local/bin"]).split(/\r?\n/u);
  assert.ok(files.includes("deepseek-stateless-mcp") && !files.includes("native-failover-probe"));
  report.productionLocalExecutables = files;

  for (const [name, environment] of [["unset", []], ["empty", ["-e", "REDIS_URL="]],
    ["whitespace", ["-e", "REDIS_URL=   "]]] as const) {
    let container: string | undefined;
    const row: Record<string, unknown> = { name, network: "none", mounts: 0, publishedPorts: 0 };
    cases.push(row);
    try {
      container = docker(["run", "--detach", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "256m", "--pids-limit", "64",
        "--label", `native-mcp-durability-run=${process.env.GITHUB_RUN_ID}`, ...environment, image]);
      assert.match(container, /^[a-f0-9]{64}$/u);
      const deadline = Date.now() + 3000;
      let observed;
      do {
        observed = JSON.parse(docker(["inspect", container]))[0];
        if (!observed.State.Running) break;
        await delay(200);
      } while (Date.now() < deadline);
      assert.equal(observed.Image, metadata.Id);
      assert.equal(observed.HostConfig.NetworkMode, "none");
      assert.deepEqual(observed.Mounts, []);
      assert.ok(!observed.HostConfig.PortBindings || Object.keys(observed.HostConfig.PortBindings).length === 0);
      assert.equal(observed.State.OOMKilled, false);
      assert.equal(observed.State.Error, "");
      row.state = observed.State;
      // Docker sends the native startup error to stderr; retain it separately.
      const log = spawnSync("docker", ["logs", "--tail", "25", container], { cwd: repoRoot, encoding: "utf8", timeout: 15_000 });
      assert.equal(log.status, 0);
      row.log = (log.stdout + log.stderr).slice(-1500);
      if (observed.State.Running) {
        row.nativeProcesses = docker(["top", container, "-eo", "pid,args"]);
        const ready = docker(["exec", container, "curl", "--silent", "--show-error", "--max-time", "2",
          "--write-out", "\n%{http_code}", "http://127.0.0.1:8010/readyz"]);
        row.readinessResponse = ready;
        const rpc = JSON.parse(docker(["exec", container, "curl", "--silent", "--show-error", "--max-time", "2",
          "-H", "content-type: application/json", "--data-binary",
          JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "server_info", arguments: {} } }),
          "http://127.0.0.1:8010/mcp"]));
        row.serverInfo = JSON.parse(rpc.result.content[0].text);
        row.refusedWithoutRedis = false;
      } else {
        row.refusedWithoutRedis = observed.State.ExitCode === 1 && /Connection refused \(os error 111\)/u.test(String(row.log));
      }
    } finally {
      if (container) docker(["rm", "--force", container]);
    }
  }
  assert.ok(cases.every((row) => row.refusedWithoutRedis === true), "native production service admitted without Redis instead of refusing startup");
  report.defaultDurableStartupQualified = true;
  await verifyInstanceIdentity();
  report.status = "PASS";
}

mkdirSync(output, { recursive: true });
try {
  await main();
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
  throw error;
} finally {
  writeFileSync(path.join(output, "default-durability.json"), `${JSON.stringify(report, null, 2)}\n`);
}
