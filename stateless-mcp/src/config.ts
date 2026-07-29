import { randomUUID } from "node:crypto";
import path from "node:path";

export interface ServiceConfig {
  host: string;
  port: number;
  instanceId: string;
  redisUrl: string;
  redisPrefix: string;
  workspaceRoot: string;
  allowedHostnames: string[];
  authToken?: string;
  leaseMs: number;
  pollMs: number;
  taskTimeoutSeconds: number;
  maxOutputBytes: number;
}

function positiveInteger(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) {
    return fallback;
  }
  const value = Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

export function loadConfig(): ServiceConfig {
  const authToken = process.env.MCP_AUTH_TOKEN?.trim();
  return {
    host: process.env.MCP_HOST?.trim() || "0.0.0.0",
    port: positiveInteger("MCP_PORT", 8010),
    instanceId: process.env.MCP_INSTANCE_ID?.trim() || randomUUID(),
    redisUrl: process.env.REDIS_URL?.trim() || "redis://127.0.0.1:6379",
    redisPrefix: process.env.REDIS_PREFIX?.trim() || "deepseek-infra:mcp:v1",
    workspaceRoot: path.resolve(process.env.MCP_WORKSPACE_ROOT?.trim() || process.cwd()),
    allowedHostnames: (process.env.MCP_ALLOWED_HOSTS || "localhost,127.0.0.1,[::1],mcp-lb")
      .split(",")
      .map((host) => host.trim())
      .filter((host) => host.length > 0),
    ...(authToken ? { authToken } : {}),
    leaseMs: positiveInteger("MCP_TASK_LEASE_MS", 15_000),
    pollMs: positiveInteger("MCP_TASK_POLL_MS", 250),
    taskTimeoutSeconds: positiveInteger("MCP_TASK_TIMEOUT_SECONDS", 600),
    maxOutputBytes: positiveInteger("MCP_MAX_OUTPUT_BYTES", 262_144),
  };
}
