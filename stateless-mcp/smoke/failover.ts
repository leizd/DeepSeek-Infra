import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import path from "node:path";

import { parseToolText, RetryingMcpClient } from "../src/retrying-client.js";
import type { TaskRecord } from "../src/task-store.js";

interface InstanceInfo {
  instanceId: string;
}

interface StartResult {
  taskId: string;
  deduplicated: boolean;
}

const repoRoot = path.resolve(import.meta.dirname, "..", "..", "..");
const composeFile = path.join(repoRoot, "docker-compose.stateless-mcp.yml");
const token = process.env.MCP_AUTH_TOKEN || "dev-change-me";
const loadBalancer = "http://127.0.0.1:8010";
const directEndpoints = [
  "http://127.0.0.1:8011/mcp",
  "http://127.0.0.1:8012/mcp",
];

function compose(...arguments_: string[]): void {
  const result = spawnSync("docker", ["compose", "-f", composeFile, ...arguments_], {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: "pipe",
  });
  if (result.status !== 0) {
    throw new Error(`docker compose ${arguments_.join(" ")} failed: ${result.stderr}`);
  }
}

async function waitForReady(): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${loadBalancer}/readyz`);
      if (response.ok) {
        return;
      }
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("stateless MCP load balancer did not become ready");
}

async function verifyRoundRobin(): Promise<string[]> {
  const instances: string[] = [];
  for (let index = 0; index < 4; index += 1) {
    const response = await fetch(`${loadBalancer}/instance`, {
      headers: { connection: "close" },
    });
    assert.equal(response.status, 200);
    instances.push(((await response.json()) as InstanceInfo).instanceId);
  }
  assert.deepEqual(new Set(instances), new Set(["mcp-instance-1", "mcp-instance-2"]));
  assert.notEqual(instances[0], instances[1], "round-robin did not alternate instances");
  return instances;
}

async function waitForTask(
  client: RetryingMcpClient,
  taskId: string,
  predicate: (task: TaskRecord) => boolean,
  timeoutMs: number,
): Promise<TaskRecord> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const call = await client.callTool("get_task", { taskId });
    const task = parseToolText<TaskRecord>(call.result);
    if (predicate(task)) {
      return task;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`task ${taskId} did not reach the expected state`);
}

async function main(): Promise<void> {
  compose("up", "-d", "--build");
  await waitForReady();
  const roundRobinEvidence = await verifyRoundRobin();
  const loadBalancedClient = new RetryingMcpClient([`${loadBalancer}/mcp`], token, 3);
  const idempotencyKey = `failover-${randomUUID()}`;
  const startedCall = await loadBalancedClient.callTool("start_test_run", {
    idempotencyKey,
    target: "stateless-mcp/fixtures/test_failover_probe.py",
    timeoutSeconds: 60,
  });
  const started = parseToolText<StartResult>(startedCall.result);
  const running = await waitForTask(
    loadBalancedClient,
    started.taskId,
    (task) => task.status === "running" && task.ownerInstance !== null,
    20_000,
  );
  const crashedInstance = running.ownerInstance;
  assert.ok(crashedInstance === "mcp-instance-1" || crashedInstance === "mcp-instance-2");
  const survivorEndpoint =
    crashedInstance === "mcp-instance-1" ? directEndpoints[1] : directEndpoints[0];
  const crashedEndpoint =
    crashedInstance === "mcp-instance-1" ? directEndpoints[0] : directEndpoints[1];
  assert.ok(survivorEndpoint !== undefined && crashedEndpoint !== undefined);

  compose("kill", crashedInstance);
  try {
    const retryingClient = new RetryingMcpClient([crashedEndpoint, survivorEndpoint], token, 2);
    const retryEvidence = await retryingClient.callTool("get_task", { taskId: started.taskId });
    assert.equal(retryEvidence.attempts, 2, "client did not retry the failed instance");

    const recovered = await waitForTask(
      retryingClient,
      started.taskId,
      (task) => task.status === "succeeded",
      90_000,
    );
    assert.ok(recovered.attempts >= 2, "task was not reclaimed after lease expiry");
    assert.notEqual(recovered.ownerInstance, crashedInstance);

    const deduplicatedCall = await retryingClient.callTool("start_test_run", {
      idempotencyKey,
      target: "stateless-mcp/fixtures/test_failover_probe.py",
      timeoutSeconds: 60,
    });
    const deduplicated = parseToolText<StartResult>(deduplicatedCall.result);
    assert.equal(deduplicated.taskId, started.taskId);
    assert.equal(deduplicated.deduplicated, true);
    console.log(
      JSON.stringify(
        {
          roundRobinInstances: roundRobinEvidence,
          crashedInstance,
          recoveredBy: recovered.ownerInstance,
          attempts: recovered.attempts,
          clientRetryAttempts: retryEvidence.attempts,
          idempotencyDeduplicated: true,
          status: "PASS",
        },
        null,
        2,
      ),
    );
  } finally {
    compose("up", "-d", crashedInstance);
  }
}

await main();
