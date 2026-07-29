import { spawn } from "node:child_process";
import { once } from "node:events";

import { resolveWorkspacePath } from "./workspace.js";

export interface CodeSearchInput {
  query: string;
  path: string;
  glob?: string;
  maxResults: number;
}

export interface CodeSearchResult {
  matches: string[];
  truncated: boolean;
}

export async function searchCode(
  workspaceRoot: string,
  input: CodeSearchInput,
  maxOutputBytes: number,
): Promise<CodeSearchResult> {
  const searchRoot = resolveWorkspacePath(workspaceRoot, input.path);
  const arguments_ = [
    "--line-number",
    "--column",
    "--no-heading",
    "--color",
    "never",
    "--fixed-strings",
    "--max-count",
    String(input.maxResults),
  ];
  if (input.glob !== undefined) {
    arguments_.push("--glob", input.glob);
  }
  arguments_.push("--", input.query, searchRoot);

  const child = spawn("rg", arguments_, {
    cwd: workspaceRoot,
    shell: false,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  let bytes = 0;
  let truncated = false;
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    if (bytes >= maxOutputBytes) {
      truncated = true;
      return;
    }
    const remaining = maxOutputBytes - bytes;
    const kept = Buffer.from(chunk).subarray(0, remaining).toString("utf8");
    bytes += Buffer.byteLength(kept);
    stdout += kept;
    truncated ||= kept.length < chunk.length;
  });
  child.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });
  const [exitCode] = (await once(child, "close")) as [number | null];
  if (exitCode !== 0 && exitCode !== 1) {
    throw new Error(`rg failed with exit code ${String(exitCode)}: ${stderr.trim()}`);
  }
  const matches = stdout
    .split(/\r?\n/u)
    .filter((line) => line.length > 0)
    .slice(0, input.maxResults);
  return { matches, truncated: truncated || matches.length >= input.maxResults };
}
