import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { toNodeHandler } from "@modelcontextprotocol/node";
import { createMcpHandler } from "@modelcontextprotocol/server";

import type { ServiceConfig } from "../src/config.js";
import { MemoryTaskStore } from "../src/memory-task-store.js";
import { createServiceMcp } from "../src/mcp-server.js";
import { parseToolText, RetryingMcpClient } from "../src/retrying-client.js";
import type { Telemetry } from "../src/telemetry.js";

test("retrying client reconnects to a healthy stateless instance", async () => {
  const store = new MemoryTaskStore();
  const telemetry: Telemetry = {
    async runTool<T>(_toolName: string, operation: () => Promise<T>): Promise<T> {
      return await operation();
    },
    async shutdown(): Promise<void> {},
  };
  const config: ServiceConfig = {
    host: "127.0.0.1",
    port: 0,
    instanceId: "healthy-instance",
    redisUrl: "redis://unused",
    redisPrefix: "test",
    workspaceRoot: process.cwd(),
    allowedHostnames: ["127.0.0.1"],
    leaseMs: 1_000,
    pollMs: 10,
    taskTimeoutSeconds: 60,
    maxOutputBytes: 32_768,
  };
  const handler = createMcpHandler(() => createServiceMcp(store, config, telemetry), {
    legacy: "stateless",
  });
  const nodeHandler = toNodeHandler(handler);
  const httpServer = createServer((req, res) => {
    void nodeHandler(req as unknown as Parameters<typeof nodeHandler>[0], res);
  });
  await new Promise<void>((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
  const address = httpServer.address();
  assert.ok(address !== null && typeof address !== "string");

  try {
    const client = new RetryingMcpClient(
      ["http://127.0.0.1:1/mcp", `http://127.0.0.1:${String(address.port)}/mcp`],
      undefined,
      2,
    );
    const call = await client.callTool("server_info", {});
    assert.equal(call.attempts, 2);
    assert.equal(call.endpoint, `http://127.0.0.1:${String(address.port)}/mcp`);
    assert.deepEqual(parseToolText<{ instanceId: string }>(call.result), {
      instanceId: "healthy-instance",
      clientSessionState: "none",
      durableTaskState: "redis",
    });
  } finally {
    httpServer.close();
    await handler.close();
  }
});
