import path from "node:path";

export function resolveWorkspacePath(workspaceRoot: string, candidate: string): string {
  if (candidate.includes("\0")) {
    throw new Error("path contains a null byte");
  }
  const resolved = path.resolve(workspaceRoot, candidate);
  const relative = path.relative(workspaceRoot, resolved);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("path must stay within the configured workspace");
  }
  return resolved;
}
