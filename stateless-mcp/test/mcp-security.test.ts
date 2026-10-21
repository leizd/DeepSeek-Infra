import assert from "node:assert/strict";
import test from "node:test";

import { Client, InMemoryTransport } from "@modelcontextprotocol/client";

import type { ServiceConfig } from "../src/config.js";
import { createServiceMcpHandler } from "../src/mcp-handler.js";
import { createServiceMcp } from "../src/mcp-server.js";
import { MemoryTaskStore } from "../src/memory-task-store.js";
import type { Telemetry } from "../src/telemetry.js";

const config: ServiceConfig = {
  host: "127.0.0.1", port: 8010, instanceId: "security-fixture",
  redisUrl: "redis://unused", redisPrefix: "security-fixture", workspaceRoot: process.cwd(),
  allowedHostnames: ["localhost"], leaseMs: 1_000, pollMs: 10,
  taskTimeoutSeconds: 60, maxOutputBytes: 32_768,
};
const telemetry: Telemetry = {
  async runTool<T>(_name: string, operation: () => Promise<T>): Promise<T> { return await operation(); },
  async shutdown(): Promise<void> {},
};

function initialization(id: string, clientName = "security-fixture"): Request {
  return new Request("http://localhost/mcp", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      "mcp-protocol-version": "2025-03-26",
    },
    body: JSON.stringify({
      jsonrpc: "2.0", id, method: "initialize",
      params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: clientName, version: "1.0.0" } },
    }),
  });
}

async function responseId(response: Response): Promise<string> {
  assert.equal(response.status, 200);
  const text = await response.text();
  const data = text.split(/\r?\n/u).find((line) => line.startsWith("data: "));
  const body = JSON.parse(data === undefined ? text : data.slice(6)) as { id: string; result?: unknown };
  assert.ok(body.result);
  return body.id;
}

test("SDK refuses oversized structured arguments before durable task creation", async () => {
  const store = new MemoryTaskStore();
  const server = createServiceMcp(store, config, telemetry);
  const client = new Client({ name: "security-fixture", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  try {
    const arguments_ = {
      idempotencyKey: "bounded-task-request", target: "tests/test_mcp.py",
      padding: new Array<number>(10_001).fill(0),
    };
    const rejected = await client.callTool({ name: "start_test_run", arguments: arguments_ });
    assert.equal(rejected.isError, true);
    const accepted = await client.callTool({
      name: "start_test_run", arguments: { idempotencyKey: arguments_.idempotencyKey, target: arguments_.target },
    });
    assert.notEqual(accepted.isError, true);
    const entry = accepted.content.find((item) => item.type === "text");
    assert.equal(entry?.type, "text");
    const payload = JSON.parse(entry?.type === "text" ? entry.text : "{}") as { deduplicated: boolean };
    assert.equal(payload.deduplicated, false);
  } finally {
    await client.close();
    await server.close();
  }
});

test("HTTP body limit refuses an oversized request and retains normal serving", async () => {
  const handler = createServiceMcpHandler(new MemoryTaskStore(), config, telemetry, () => undefined);
  try {
    const rejected = await handler.fetch(initialization("oversized", "x".repeat(1024 * 1024)));
    assert.equal(rejected.status, 413);
    await rejected.text();
    assert.equal(await responseId(await handler.fetch(initialization("after-refusal"))), "after-refusal");
  } finally {
    await handler.close();
  }
});

test("parallel stateless requests retain their own response identities", async () => {
  const handler = createServiceMcpHandler(new MemoryTaskStore(), config, telemetry, () => undefined);
  try {
    const [first, second] = await Promise.all([
      handler.fetch(initialization("client-one", "one")), handler.fetch(initialization("client-two", "two")),
    ]);
    assert.deepEqual(await Promise.all([responseId(first), responseId(second)]), ["client-one", "client-two"]);
    assert.equal(await responseId(await handler.fetch(initialization("third-request"))), "third-request");
  } finally {
    await handler.close();
  }
});
