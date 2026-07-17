#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const APP_ROOT = process.env.OPENCLAW_PRUNE_APP_ROOT || "/app";
const TEAMAGENT_ROOT =
  process.env.OPENCLAW_PRUNE_TEAMAGENT_ROOT || "/opt/teamagent";
const DIST_ROOT = path.join(APP_ROOT, "dist");
const SKILLS_ROOT = path.join(APP_ROOT, "skills");
const PLUGIN_ROOTS = [
  path.join(TEAMAGENT_ROOT, "plugins", "slack"),
  path.join(TEAMAGENT_ROOT, "plugins", "amazon-bedrock"),
];
const REPORT_PATH = path.join(TEAMAGENT_ROOT, "runtime-prune-report.json");

const require = createRequire(import.meta.url);
const { init, parse } = require(
  path.join(APP_ROOT, "node_modules", "es-module-lexer"),
);
await init;

const toContainerPath = (candidate) =>
  candidate.replaceAll(path.sep, "/");

function readJson(candidate) {
  return JSON.parse(fs.readFileSync(candidate, "utf8"));
}

function writeJson(candidate, value) {
  fs.writeFileSync(candidate, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o444,
  });
}

function lstatOrNull(candidate) {
  try {
    return fs.lstatSync(candidate);
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

function listFiles(root, predicate = () => true) {
  const files = [];
  const walk = (candidate) => {
    const stat = lstatOrNull(candidate);
    if (!stat || stat.isSymbolicLink()) return;
    if (stat.isFile()) {
      if (predicate(candidate)) files.push(candidate);
      return;
    }
    if (!stat.isDirectory()) return;
    for (const entry of fs.readdirSync(candidate)) {
      walk(path.join(candidate, entry));
    }
  };
  walk(root);
  return files;
}

for (const target of [
  path.join(DIST_ROOT, "extensions", "browser"),
  path.join(DIST_ROOT, "extensions", "codex"),
  path.join(DIST_ROOT, "extensions", "codex-supervisor"),
  path.join(APP_ROOT, "extensions"),
  path.join(APP_ROOT, "pnpm-workspace.yaml"),
  path.join(APP_ROOT, "src"),
  path.join(APP_ROOT, "node_modules", ".bin"),
  path.join(APP_ROOT, "node_modules", ".modules.yaml"),
  path.join(APP_ROOT, "node_modules", ".pnpm"),
  path.join(APP_ROOT, "node_modules", ".pnpm-workspace-state-v1.json"),
  path.join(APP_ROOT, "node_modules", ".tmp"),
  path.join(APP_ROOT, "node_modules", ".unrun"),
]) {
  fs.rmSync(target, { recursive: true, force: true });
}

function collectModuleGraph() {
  const moduleFiles = listFiles(
    DIST_ROOT,
    (candidate) => /\.(?:c|m)?js$/u.test(candidate),
  );
  const moduleSet = new Set(moduleFiles);
  const dependencies = new Map();

  for (const modulePath of moduleFiles) {
    const source = fs.readFileSync(modulePath, "utf8");
    const resolvedImports = [];
    for (const record of parse(source)[0]) {
      if (!record.n?.startsWith(".")) continue;
      const cleanSpecifier = record.n.replace(/[?#].*$/u, "");
      const unresolved = path.resolve(path.dirname(modulePath), cleanSpecifier);
      const resolved = [
        unresolved,
        `${unresolved}.js`,
        `${unresolved}.mjs`,
        `${unresolved}.cjs`,
        path.join(unresolved, "index.js"),
      ].find((candidate) => moduleSet.has(candidate));
      if (resolved) resolvedImports.push(resolved);
    }
    dependencies.set(modulePath, resolvedImports);
  }

  const roots = ["entry.js", "index.js"]
    .map((name) => path.join(DIST_ROOT, name))
    .filter((candidate) => moduleSet.has(candidate));
  if (roots.length !== 2) {
    throw new Error("expected both dist/entry.js and dist/index.js");
  }

  const reachable = new Set();
  const pending = [...roots];
  while (pending.length > 0) {
    const current = pending.pop();
    if (reachable.has(current)) continue;
    reachable.add(current);
    pending.push(...(dependencies.get(current) || []));
  }
  return { moduleFiles, dependencies, reachable, roots };
}

const browserNamePattern = /(?:browser|playwright|chrome-mcp)/iu;
const browserImplementationPattern =
  /(?:\/\/#region extensions\/browser\/|function registerBrowserPlugin\(|registerBrowserCli\(program|createBrowserPluginService\()/u;

const initialGraph = collectModuleGraph();
const removedBrowserChunks = [];
for (const modulePath of initialGraph.moduleFiles) {
  const source = fs.readFileSync(modulePath, "utf8");
  const isBrowserCandidate =
    source.includes("extensions/browser/") ||
    browserNamePattern.test(path.basename(modulePath));
  if (isBrowserCandidate && !initialGraph.reachable.has(modulePath)) {
    removedBrowserChunks.push(toContainerPath(modulePath));
    fs.rmSync(modulePath);
  }
}
removedBrowserChunks.sort();

const cliMetadataPath = path.join(DIST_ROOT, "cli-startup-metadata.json");
const cliMetadata = readJson(cliMetadataPath);
delete cliMetadata.browserHelpSourceSignature;
delete cliMetadata.browserHelpText;
writeJson(cliMetadataPath, cliMetadata);

const finalGraph = collectModuleGraph();
const residualDeadBrowserChunks = [];
const browserRegistrationChunks = [];
const sharedBrowserChunks = [];
const residualBrowserSidecarMarkers = [];
for (const modulePath of finalGraph.moduleFiles) {
  const source = fs.readFileSync(modulePath, "utf8");
  const browserCandidate =
    source.includes("extensions/browser/") ||
    browserNamePattern.test(path.basename(modulePath));
  if (browserCandidate && !finalGraph.reachable.has(modulePath)) {
    residualDeadBrowserChunks.push(toContainerPath(modulePath));
  }
  if (
    finalGraph.reachable.has(modulePath) &&
    browserNamePattern.test(path.basename(modulePath))
  ) {
    sharedBrowserChunks.push(toContainerPath(modulePath));
  }
  if (
    finalGraph.reachable.has(modulePath) &&
    browserImplementationPattern.test(source)
  ) {
    browserRegistrationChunks.push(toContainerPath(modulePath));
  }
  if (source.includes("extensions/browser/")) {
    const allowedSidecarMarker =
      finalGraph.reachable.has(modulePath) &&
      source.includes("//#region scripts/lib/bundled-runtime-sidecar-paths.json") &&
      source.includes('"dist/extensions/browser/runtime-api.js"') &&
      !browserImplementationPattern.test(source);
    if (!allowedSidecarMarker) {
      residualBrowserSidecarMarkers.push(toContainerPath(modulePath));
    }
  }
}

if (removedBrowserChunks.length === 0) {
  throw new Error("browser implementation pruning removed no chunks");
}
if (
  !removedBrowserChunks.some((candidate) =>
    path.basename(candidate).startsWith("plugin-registration-"),
  )
) {
  throw new Error("browser plugin registration chunk was not pruned");
}
if (
  residualDeadBrowserChunks.length > 0 ||
  browserRegistrationChunks.length > 0 ||
  residualBrowserSidecarMarkers.length > 0
) {
  throw new Error(
    `browser reachability contract failed: ${JSON.stringify({
      residualDeadBrowserChunks,
      browserRegistrationChunks,
      residualBrowserSidecarMarkers,
    })}`,
  );
}

const forbiddenNames = new Set([
  "@openclaw/browser-plugin",
  "@typescript/native-preview",
  "esbuild",
  "jiti",
  "jscpd",
  "oxfmt",
  "oxlint",
  "oxlint-tsgolint",
  "playwright",
  "playwright-core",
  "rolldown",
  "rollup",
  "ts-node",
  "tsdown",
  "tsx",
  "typescript",
  "vite",
  "vitest",
]);
const forbiddenPrefixes = [
  "@esbuild/",
  "@openai/codex",
  "@rolldown/",
  "@rollup/",
  "@types/",
  "@vitest/",
];
const isForbidden = (name) =>
  forbiddenNames.has(name) ||
  forbiddenPrefixes.some((prefix) => name.startsWith(prefix));

const forbiddenPackagesRemoved = [];
function normalizePackageMetadata(packagePath, metadata, keepBin = false) {
  delete metadata.devDependencies;
  delete metadata.scripts;
  delete metadata.types;
  delete metadata.typings;
  delete metadata.typesVersions;
  if (!keepBin) delete metadata.bin;
  for (const section of [
    "dependencies",
    "optionalDependencies",
    "peerDependencies",
    "peerDependenciesMeta",
  ]) {
    for (const name of Object.keys(metadata[section] || {})) {
      if (isForbidden(name)) delete metadata[section][name];
    }
    if (metadata[section] && Object.keys(metadata[section]).length === 0) {
      delete metadata[section];
    }
  }
  fs.writeFileSync(packagePath, `${JSON.stringify(metadata, null, 2)}\n`);
}

function pruneAndNormalize(root) {
  const stat = lstatOrNull(root);
  if (!stat || stat.isSymbolicLink() || !stat.isDirectory()) return;
  const packagePath = path.join(root, "package.json");
  let metadata = null;
  try {
    metadata = readJson(packagePath);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (metadata && isForbidden(metadata.name || "")) {
    forbiddenPackagesRemoved.push({
      path: toContainerPath(root),
      name: metadata.name,
      version: metadata.version || null,
    });
    fs.rmSync(root, { recursive: true, force: true });
    return;
  }
  for (const entry of fs.readdirSync(root)) {
    pruneAndNormalize(path.join(root, entry));
  }
  if (metadata) {
    normalizePackageMetadata(
      packagePath,
      metadata,
      packagePath === path.join(APP_ROOT, "package.json"),
    );
  }
}

for (const root of [
  path.join(APP_ROOT, "node_modules"),
  ...PLUGIN_ROOTS.map((root) => path.join(root, "node_modules")),
]) {
  pruneAndNormalize(root);
}
for (const packageRoot of [APP_ROOT, ...PLUGIN_ROOTS]) {
  const packagePath = path.join(packageRoot, "package.json");
  const metadata = readJson(packagePath);
  normalizePackageMetadata(
    packagePath,
    metadata,
    packagePath === path.join(APP_ROOT, "package.json"),
  );
}

function resolveDependency(fromPackageRoot, dependencyName) {
  let cursor = fromPackageRoot;
  while (true) {
    const candidate = path.join(cursor, "node_modules", dependencyName);
    if (lstatOrNull(path.join(candidate, "package.json"))) return candidate;
    const parent = path.dirname(cursor);
    if (parent === cursor) return null;
    cursor = parent;
  }
}

const markedPackageRealPaths = new Set();
const unresolvedRequiredDependencies = [];
function markProductionPackage(packageRoot) {
  const realPackageRoot = fs.realpathSync(packageRoot);
  if (markedPackageRealPaths.has(realPackageRoot)) return;
  markedPackageRealPaths.add(realPackageRoot);
  const metadata = readJson(path.join(packageRoot, "package.json"));
  for (const section of [
    "dependencies",
    "optionalDependencies",
    "peerDependencies",
  ]) {
    for (const dependencyName of Object.keys(metadata[section] || {})) {
      const dependencyRoot = resolveDependency(packageRoot, dependencyName);
      if (dependencyRoot) {
        markProductionPackage(dependencyRoot);
        continue;
      }
      const optional =
        section === "optionalDependencies" ||
        (section === "peerDependencies" &&
          metadata.peerDependenciesMeta?.[dependencyName]?.optional === true);
      if (!optional) {
        unresolvedRequiredDependencies.push({
          package: `${metadata.name}@${metadata.version}`,
          dependency: dependencyName,
          section,
        });
      }
    }
  }
}

for (const packageRoot of [APP_ROOT, ...PLUGIN_ROOTS]) {
  markProductionPackage(packageRoot);
}
if (unresolvedRequiredDependencies.length > 0) {
  throw new Error(
    `required production dependencies are missing: ${JSON.stringify(
      unresolvedRequiredDependencies,
    )}`,
  );
}

const installedPackageRoots = [];
function collectNodeModules(nodeModulesRoot) {
  const stat = lstatOrNull(nodeModulesRoot);
  if (!stat || stat.isSymbolicLink() || !stat.isDirectory()) return;
  for (const entry of fs.readdirSync(nodeModulesRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
    const candidate = path.join(nodeModulesRoot, entry.name);
    if (entry.name.startsWith("@") && !entry.isSymbolicLink()) {
      for (const scopedEntry of fs.readdirSync(candidate, {
        withFileTypes: true,
      })) {
        if (!scopedEntry.isDirectory() && !scopedEntry.isSymbolicLink()) {
          continue;
        }
        collectPackageRoot(path.join(candidate, scopedEntry.name));
      }
      continue;
    }
    collectPackageRoot(candidate);
  }
}

function collectPackageRoot(packageRoot) {
  if (!lstatOrNull(path.join(packageRoot, "package.json"))) return;
  installedPackageRoots.push(packageRoot);
  const stat = lstatOrNull(packageRoot);
  if (!stat?.isSymbolicLink()) {
    collectNodeModules(path.join(packageRoot, "node_modules"));
  }
}

for (const nodeModulesRoot of [
  path.join(APP_ROOT, "node_modules"),
  ...PLUGIN_ROOTS.map((root) => path.join(root, "node_modules")),
]) {
  collectNodeModules(nodeModulesRoot);
}

const orphanPackagesRemoved = [];
for (const packageRoot of installedPackageRoots.toSorted(
  (left, right) => right.length - left.length,
)) {
  const realPackageRoot = fs.realpathSync(packageRoot);
  if (markedPackageRealPaths.has(realPackageRoot)) continue;
  const metadata = readJson(path.join(packageRoot, "package.json"));
  orphanPackagesRemoved.push({
    path: toContainerPath(packageRoot),
    name: metadata.name,
    version: metadata.version || null,
  });
  fs.rmSync(packageRoot, { recursive: true, force: true });
}

for (const nodeModulesRoot of [
  path.join(APP_ROOT, "node_modules"),
  ...PLUGIN_ROOTS.map((root) => path.join(root, "node_modules")),
]) {
  const stat = lstatOrNull(nodeModulesRoot);
  if (!stat?.isDirectory() || stat.isSymbolicLink()) continue;
  for (const entry of fs.readdirSync(nodeModulesRoot)) {
    const candidate = path.join(nodeModulesRoot, entry);
    if (
      entry.startsWith("@") &&
      lstatOrNull(candidate)?.isDirectory() &&
      fs.readdirSync(candidate).length === 0
    ) {
      fs.rmdirSync(candidate);
    }
  }
}

const developmentDirectoryPattern =
  /^(?:__fixtures__|__snapshots__|__tests__|bench(?:marks?)?|coverage|examples?|fixtures?|specs?|tests?)$/iu;
const developmentFilePatterns = [
  /\.(?:d\.)?(?:cts|mts|ts|tsx)$/iu,
  /\.map$/iu,
  /\.(?:bench|benchmark|test|spec)\.(?:cjs|js|jsx|mjs|ts|tsx)$/iu,
  /^(?:bench|benchmark|test|tests|spec)\.(?:cjs|js|jsx|mjs|ts|tsx)$/iu,
  /\.snap$/iu,
  /\.flow$/iu,
  /^(?:tsconfig(?:\.[^.]+)?\.json|vite\.config\..+|vitest\.config\..+)$/iu,
];
const developmentPayloadRemoved = [];
function pruneDevelopmentPayload(root) {
  const stat = lstatOrNull(root);
  if (!stat || stat.isSymbolicLink()) return;
  if (stat.isFile()) {
    const basename = path.basename(root);
    if (developmentFilePatterns.some((pattern) => pattern.test(basename))) {
      developmentPayloadRemoved.push(toContainerPath(root));
      fs.rmSync(root);
    }
    return;
  }
  if (!stat.isDirectory()) return;
  if (developmentDirectoryPattern.test(path.basename(root))) {
    developmentPayloadRemoved.push(`${toContainerPath(root)}/`);
    fs.rmSync(root, { recursive: true, force: true });
    return;
  }
  for (const entry of fs.readdirSync(root)) {
    pruneDevelopmentPayload(path.join(root, entry));
  }
}

for (const root of [
  DIST_ROOT,
  SKILLS_ROOT,
  path.join(APP_ROOT, "node_modules"),
  ...PLUGIN_ROOTS,
]) {
  pruneDevelopmentPayload(root);
}

const forbiddenPackagesRemaining = [];
const packageInstances = [];
const residualBinDeclarations = [];
function inventoryPackages(root) {
  const stat = lstatOrNull(root);
  if (!stat || stat.isSymbolicLink() || !stat.isDirectory()) return;
  const packagePath = path.join(root, "package.json");
  try {
    const metadata = readJson(packagePath);
    if (metadata.name && metadata.version) {
      packageInstances.push({
        path: toContainerPath(packagePath),
        name: metadata.name,
        version: metadata.version,
      });
    }
    if (isForbidden(metadata.name || "")) {
      forbiddenPackagesRemaining.push({
        path: toContainerPath(root),
        name: metadata.name,
        version: metadata.version || null,
      });
    }
    if (
      metadata.bin &&
      packagePath !== path.join(APP_ROOT, "package.json")
    ) {
      residualBinDeclarations.push({
        path: toContainerPath(packagePath),
        name: metadata.name || null,
        bin: metadata.bin,
      });
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  for (const entry of fs.readdirSync(root)) {
    inventoryPackages(path.join(root, entry));
  }
}
inventoryPackages(APP_ROOT);
inventoryPackages(TEAMAGENT_ROOT);

const residualDevelopmentPayload = [];
function findDevelopmentPayload(root) {
  const stat = lstatOrNull(root);
  if (!stat || stat.isSymbolicLink()) return;
  const basename = path.basename(root);
  if (
    (stat.isDirectory() && developmentDirectoryPattern.test(basename)) ||
    (stat.isFile() &&
      developmentFilePatterns.some((pattern) => pattern.test(basename)))
  ) {
    residualDevelopmentPayload.push(toContainerPath(root));
    return;
  }
  if (stat.isDirectory()) {
    for (const entry of fs.readdirSync(root)) {
      findDevelopmentPayload(path.join(root, entry));
    }
  }
}
for (const root of [
  DIST_ROOT,
  SKILLS_ROOT,
  path.join(APP_ROOT, "node_modules"),
  ...PLUGIN_ROOTS,
]) {
  findDevelopmentPayload(root);
}

if (
  forbiddenPackagesRemaining.length > 0 ||
  residualBinDeclarations.length > 0 ||
  residualDevelopmentPayload.length > 0
) {
  throw new Error(
    `runtime payload pruning failed: ${JSON.stringify({
      forbiddenPackagesRemaining,
      residualBinDeclarations,
      residualDevelopmentPayload,
    })}`,
  );
}

writeJson(REPORT_PATH, {
  schemaVersion: 1,
  browser: {
    graphRoots: finalGraph.roots.map(toContainerPath).toSorted(),
    totalModuleCount: finalGraph.moduleFiles.length,
    reachableModuleCount: finalGraph.reachable.size,
    removedImplementationChunks: removedBrowserChunks,
    residualUnreachableBrowserCandidates: 0,
    reachableRegistrationChunks: 0,
    sharedReachableChunks: sharedBrowserChunks.toSorted(),
    sidecarPathMarkersValidatedAsDataOnly: 1,
    cliHelpMetadataRemoved: true,
  },
  packages: {
    forbiddenRemoved: forbiddenPackagesRemoved.toSorted((left, right) =>
      left.path.localeCompare(right.path),
    ),
    orphanProductionClosureRemoved: orphanPackagesRemoved.toSorted(
      (left, right) => left.path.localeCompare(right.path),
    ),
    retainedInstances: packageInstances.toSorted((left, right) =>
      left.path.localeCompare(right.path),
    ),
    residualForbidden: 0,
    residualNonRootBinDeclarations: 0,
  },
  developmentPayload: {
    removedPathCount: developmentPayloadRemoved.length,
    residualPathCount: 0,
  },
});
