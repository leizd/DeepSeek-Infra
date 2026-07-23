import { createHash } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

interface ViteChunkMetadata {
  importedCss?: Set<string>;
}

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

function workspaceAssetManifest(): Plugin {
  return {
    name: "workspace-asset-manifest",
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
      const buildId = createHash("sha256").update(runtimeAssets.join("\n")).digest("hex").slice(0, 16);
      const withUiBase = (fileName: string) => `/ui/${fileName}`;
      writeFileSync(
        resolve(options.dir, "workspace-assets.json"),
        `${JSON.stringify({
          buildId,
          core: runtimeAssets.filter((fileName) => core.has(fileName)).map(withUiBase),
          offlinePrimary: offlinePrimary.map(withUiBase),
          recovery: recovery.map(withUiBase),
          routeOptional: runtimeAssets.filter((fileName) => !classified.has(fileName)).map(withUiBase),
        }, null, 2)}\n`,
        "utf8",
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), workspaceAssetManifest()],
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
