import assert from "node:assert/strict";
import test from "node:test";

import { Client, InMemoryTransport } from "@modelcontextprotocol/client";

import type { ServiceConfig } from "../src/config.js";
import { MemoryTaskStore } from "../src/memory-task-store.js";
import { createServiceMcp } from "../src/mcp-server.js";
import type { Telemetry } from "../src/telemetry.js";

const config: ServiceConfig = {
  host: "127.0.0.1",
  port: 8010,
  instanceId: "test-instance",
  redisUrl: "redis://unused",
  redisPrefix: "test",
  workspaceRoot: process.cwd(),
  allowedHostnames: ["localhost"],
  leaseMs: 1_000,
  pollMs: 10,
  taskTimeoutSeconds: 60,
  maxOutputBytes: 32_768,
};

const telemetry: Telemetry = {
  async runTool<T>(_toolName: string, operation: () => Promise<T>): Promise<T> {
    return await operation();
  },
  async shutdown(): Promise<void> {},
};

test("official SDK exposes stateless tools and idempotent task creation", async () => {
  const store = new MemoryTaskStore();
  const server = createServiceMcp(store, config, telemetry);
  const client = new Client({ name: "unit-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const tools = await client.listTools();
    assert.deepEqual(
      new Set(tools.tools.map((tool) => tool.name)),
      new Set(["server_info", "code_search", "start_test_run", "get_task", "query_logs"]),
    );

    const info = await client.callTool({ name: "server_info", arguments: {} });
    const infoText = info.content.find((entry) => entry.type === "text");
    assert.equal(infoText?.type, "text");
    assert.deepEqual(JSON.parse(infoText?.type === "text" ? infoText.text : "{}"), {
      instanceId: "test-instance",
      clientSessionState: "none",
      durableTaskState: "redis",
    });

    const first = await client.callTool({
      name: "start_test_run",
      arguments: {
        idempotencyKey: "unit-request-123",
        target: "tests/test_mcp.py",
        timeoutSeconds: 30,
      },
    });
    const replay = await client.callTool({
      name: "start_test_run",
      arguments: {
        idempotencyKey: "unit-request-123",
        target: "tests/test_mcp.py",
        timeoutSeconds: 30,
      },
    });
    const firstText = first.content.find((entry) => entry.type === "text");
    const replayText = replay.content.find((entry) => entry.type === "text");
    const firstPayload = JSON.parse(firstText?.type === "text" ? firstText.text : "{}") as {
      taskId: string;
      deduplicated: boolean;
    };
    const replayPayload = JSON.parse(replayText?.type === "text" ? replayText.text : "{}") as {
      taskId: string;
      deduplicated: boolean;
    };
    assert.equal(replayPayload.taskId, firstPayload.taskId);
    assert.equal(firstPayload.deduplicated, false);
    assert.equal(replayPayload.deduplicated, true);
  } finally {
    await client.close();
    await server.close();
  }
});
