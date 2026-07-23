import { createHash } from "node:crypto";
import {
  existsSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { relative, resolve } from "node:path";
import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

import {
  createFrontendBuildIdentity,
  resolveFrontendSourceRevision,
  type FrontendBuildIdentity,
} from "./buildIdentity";

interface ViteChunkMetadata {
  importedCss?: Set<string>;
}

interface WorkspaceAssetManifest extends FrontendBuildIdentity {
  assetSetDigest: string;
  core: string[];
  offlinePrimary: string[];
  recovery: string[];
  routeOptional: string[];
}

const FRONTEND_DIR = fileURLToPath(new URL(".", import.meta.url));
const REPOSITORY_ROOT = resolve(FRONTEND_DIR, "..");
const FRONTEND_PACKAGE = JSON.parse(readFileSync(resolve(FRONTEND_DIR, "package.json"), "utf8")) as { version: string };
const BUILD_IDENTITY = createFrontendBuildIdentity({
  version: FRONTEND_PACKAGE.version,
  sourceRevision: resolveFrontendSourceRevision(REPOSITORY_ROOT, FRONTEND_DIR),
});
const WORKER_BUILD_ID_TOKEN = "__DEEPSEEK_WORKER_BUILD_ID__";
const WORKER_ASSET_DIGEST_TOKEN = "__DEEPSEEK_WORKER_ASSET_SET_DIGEST__";
const WORKER_MANIFEST_URL_TOKEN = "__DEEPSEEK_WORKER_MANIFEST_URL__";

const WORKSPACE_PRIMARY_MODULES = [
  "src/features/settings/ConnectionSettingsFeature.tsx",
  "src/features/projects/ProjectsFeature.tsx",
  "src/features/skills/SkillsFeature.tsx",
  "src/features/skills/SkillsRuntimeBoundary.tsx",
  "src/features/memory/MemoryFeature.tsx",
  "src/features/reminders/RemindersFeature.tsx",
  "src/features/diagnostics/DiagnosticsFeature.tsx",
  "src/features/file-reader/FilePreviewFeature.tsx",
  "src/features/file-reader/ImageLightboxFeature.tsx",
  "src/features/activity/ActivityFeature.tsx",
];

function outputFiles(root: string, current = root): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    const path = resolve(current, entry.name);
    if (entry.isDirectory()) files.push(...outputFiles(root, path));
    else if (entry.isFile()) files.push(path);
  }
  return files.sort((left, right) => relative(root, left).localeCompare(relative(root, right)));
}

function assetSetDigest(
  outputDirectory: string,
  manifest: Omit<WorkspaceAssetManifest, "assetSetDigest">,
  workerTemplates: Record<string, string>,
): string {
  const excluded = /^(?:sw(?:-root)?(?:-[0-9a-f]{16})?\.js|workspace-assets(?:-[0-9a-f]{16})?\.json)$/;
  const hash = createHash("sha256");
  for (const path of outputFiles(outputDirectory)) {
    const name = relative(outputDirectory, path).replaceAll("\\", "/");
    if (excluded.test(name) || name.endsWith(".map")) continue;
    hash.update(`asset:${name}\0`);
    hash.update(readFileSync(path));
    hash.update("\0");
  }
  hash.update("manifest:\0");
  hash.update(JSON.stringify(manifest));
  hash.update("\0");
  for (const [name, source] of Object.entries(workerTemplates).sort(([left], [right]) => left.localeCompare(right))) {
    hash.update(`worker-template:${name}\0${source}\0`);
  }
  return hash.digest("hex");
}

function renderWorker(
  template: string,
  identity: FrontendBuildIdentity,
  digest: string,
): string {
  return template
    .replaceAll(WORKER_BUILD_ID_TOKEN, identity.buildId)
    .replaceAll(WORKER_ASSET_DIGEST_TOKEN, digest)
    .replaceAll(WORKER_MANIFEST_URL_TOKEN, `/ui/workspace-assets-${identity.buildId}.json`);
}

export function workspaceAssetManifest(identity: FrontendBuildIdentity = BUILD_IDENTITY): Plugin {
  return {
    name: "workspace-asset-manifest",
    transformIndexHtml(html) {
      return html.replace(
        '<meta name="deepseek-infra-version"',
        `<meta name="deepseek-infra-build-id" content="${identity.buildId}" />\n    <meta name="deepseek-infra-source-revision" content="${identity.sourceRevision}" />\n    <meta name="deepseek-infra-version"`,
      );
    },
    writeBundle(options, bundle) {
      if (!options.dir) throw new Error("workspace asset manifest requires an output directory");
      const core = new Set<string>();
      const offlinePrimaryGraph = new Set<string>();
      const recoveryGraph = new Set<string>();

      const collectChunk = (target: Set<string>, fileName: string) => {
        if (target.has(fileName)) return;
        const output = bundle[fileName];
        if (!output || output.type !== "chunk") return;
        target.add(fileName);
        output.imports.forEach((dependency) => collectChunk(target, dependency));
        const metadata = output.viteMetadata as ViteChunkMetadata | undefined;
        metadata?.importedCss?.forEach((css) => target.add(css));
      };

      Object.values(bundle)
        .filter((output) => output.type === "chunk" && output.isEntry)
        .forEach((output) => collectChunk(core, output.fileName));

      Object.values(bundle)
        .forEach((output) => {
          if (output.type !== "chunk" || !output.isDynamicEntry) return;
          const moduleId = output.facadeModuleId?.replaceAll("\\", "/") ?? "";
          if (!WORKSPACE_PRIMARY_MODULES.some((candidate) => moduleId.includes(candidate))) return;
          collectChunk(
            moduleId.includes("?workspace-retry") ? recoveryGraph : offlinePrimaryGraph,
            output.fileName,
          );
        });

      const runtimeAssets = Object.entries(bundle)
        .filter(([fileName, output]) =>
          existsSync(resolve(options.dir!, fileName)) && (
            fileName.endsWith(".css") ||
            (fileName.endsWith(".js") && output.type === "chunk" && output.code.trim().length > 0)
          ),
        )
        .map(([fileName]) => fileName)
        .sort();
      const offlinePrimary = runtimeAssets.filter((fileName) => offlinePrimaryGraph.has(fileName) && !core.has(fileName));
      const recovery = runtimeAssets.filter(
        (fileName) => recoveryGraph.has(fileName) && !core.has(fileName) && !offlinePrimaryGraph.has(fileName),
      );
      const classified = new Set([...core, ...offlinePrimary, ...recovery]);
      const withUiBase = (fileName: string) => `/ui/${fileName}`;
      const workerTemplates = {
        "sw.js": readFileSync(resolve(options.dir, "sw.js"), "utf8"),
        "sw-root.js": readFileSync(resolve(options.dir, "sw-root.js"), "utf8"),
      };
      const manifestBase: Omit<WorkspaceAssetManifest, "assetSetDigest"> = {
        ...identity,
        core: runtimeAssets.filter((fileName) => core.has(fileName)).map(withUiBase),
        offlinePrimary: offlinePrimary.map(withUiBase),
        recovery: recovery.map(withUiBase),
        routeOptional: runtimeAssets.filter((fileName) => !classified.has(fileName)).map(withUiBase),
      };
      const digest = assetSetDigest(options.dir, manifestBase, workerTemplates);
      const manifest: WorkspaceAssetManifest = { ...manifestBase, assetSetDigest: digest };
      const serializedManifest = `${JSON.stringify(manifest, null, 2)}\n`;
      writeFileSync(
        resolve(options.dir, "workspace-assets.json"),
        serializedManifest,
        "utf8",
      );
      writeFileSync(
        resolve(options.dir, `workspace-assets-${identity.buildId}.json`),
        serializedManifest,
        "utf8",
      );
      writeFileSync(
        resolve(options.dir, `sw-${identity.buildId}.js`),
        renderWorker(workerTemplates["sw.js"], identity, digest),
        "utf8",
      );
      writeFileSync(
        resolve(options.dir, `sw-root-${identity.buildId}.js`),
        renderWorker(workerTemplates["sw-root.js"], identity, digest),
        "utf8",
      );
      rmSync(resolve(options.dir, "sw.js"));
      rmSync(resolve(options.dir, "sw-root.js"));
    },
  };
}

export default defineConfig({
  plugins: [react(), workspaceAssetManifest()],
  define: {
    __APP_BUILD_ID__: JSON.stringify(BUILD_IDENTITY.buildId),
    __APP_SOURCE_REVISION__: JSON.stringify(BUILD_IDENTITY.sourceRevision),
  },
  base: "/ui/",
  build: {
    outDir: fileURLToPath(new URL("../static/ui", import.meta.url)),
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
      "/readyz": "http://127.0.0.1:8000",
    },
  },
});
