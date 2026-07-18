#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { builtinModules, createRequire } from "node:module";

const APP_ROOT = process.env.OPENCLAW_PRUNE_APP_ROOT || "/app";
const TEAMAGENT_ROOT =
  process.env.OPENCLAW_PRUNE_TEAMAGENT_ROOT || "/opt/teamagent";
const DIST_ROOT = path.join(APP_ROOT, "dist");
const CONTROL_UI_ROOT = path.join(DIST_ROOT, "control-ui");
const CONTROL_UI_INDEX = path.join(CONTROL_UI_ROOT, "index.html");
const SKILLS_ROOT = path.join(APP_ROOT, "skills");
const PLUGIN_ROOTS = [
  path.join(TEAMAGENT_ROOT, "plugins", "slack"),
  path.join(TEAMAGENT_ROOT, "plugins", "amazon-bedrock"),
];
const REPORT_PATH = path.join(TEAMAGENT_ROOT, "runtime-prune-report.json");
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
function isForbiddenPackageName(name) {
  return (
    forbiddenNames.has(name) ||
    forbiddenPrefixes.some((prefix) => name.startsWith(prefix))
  );
}

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

function computeProductionPackageClosure(reachableForbiddenPackageNames) {
  const markedRealPaths = new Set();
  const retained = [];
  const unresolvedRequired = [];
  const forbiddenRequired = [];
  const excludedForbiddenDeclarations = [];

  function mark(packageRoot, requestedBy = null) {
    const realPackageRoot = fs.realpathSync(packageRoot);
    if (markedRealPaths.has(realPackageRoot)) return;
    markedRealPaths.add(realPackageRoot);
    const metadata = readJson(path.join(packageRoot, "package.json"));
    retained.push({
      path: toContainerPath(packageRoot),
      realPath: toContainerPath(realPackageRoot),
      name: metadata.name,
      version: metadata.version || null,
      requestedBy,
    });
    for (const section of [
      "dependencies",
      "optionalDependencies",
      "peerDependencies",
    ]) {
      for (const dependencyName of Object.keys(metadata[section] || {})) {
        const dependencyRoot = resolveDependency(packageRoot, dependencyName);
        const optional =
          section === "optionalDependencies" ||
          (section === "peerDependencies" &&
            metadata.peerDependenciesMeta?.[dependencyName]?.optional === true);
        if (isForbiddenPackageName(dependencyName)) {
          if (reachableForbiddenPackageNames.has(dependencyName)) {
            forbiddenRequired.push({
              package: `${metadata.name}@${metadata.version}`,
              dependency: dependencyName,
              section,
            });
          } else {
            excludedForbiddenDeclarations.push({
              package: `${metadata.name}@${metadata.version}`,
              dependency: dependencyName,
              section,
              optional,
              exclusionProof:
                "not referenced by any reachable literal bare import; representative runtime operations must pass after removal",
            });
          }
          continue;
        }
        if (!dependencyRoot) {
          if (!optional) {
            unresolvedRequired.push({
              package: `${metadata.name}@${metadata.version}`,
              dependency: dependencyName,
              section,
            });
          }
          continue;
        }
        mark(
          dependencyRoot,
          `${metadata.name}@${metadata.version}:${section}:${dependencyName}`,
        );
      }
    }
  }

  for (const packageRoot of [APP_ROOT, ...PLUGIN_ROOTS]) {
    mark(packageRoot);
  }
  if (unresolvedRequired.length > 0 || forbiddenRequired.length > 0) {
    throw new Error(
      `pre-prune production dependency closure failed: ${JSON.stringify({
        unresolvedRequired,
        forbiddenRequired,
      })}`,
    );
  }
  retained.sort((left, right) => left.realPath.localeCompare(right.realPath));
  excludedForbiddenDeclarations.sort(
    (left, right) =>
      left.package.localeCompare(right.package) ||
      left.dependency.localeCompare(right.dependency) ||
      left.section.localeCompare(right.section),
  );
  return { markedRealPaths, retained, excludedForbiddenDeclarations };
}

function resolveModuleFile(importer, specifier) {
  if (
    specifier.startsWith("node:") ||
    builtinModules.includes(specifier) ||
    builtinModules.includes(`node:${specifier}`)
  ) {
    return {
      kind: "builtin",
      path: specifier.startsWith("node:") ? specifier : `node:${specifier}`,
    };
  }
  try {
    const resolved = createRequire(importer).resolve(specifier);
    if (resolved.startsWith("node:")) {
      return { kind: "builtin", path: resolved };
    }
    const stat = lstatOrNull(resolved);
    if (!stat?.isFile()) return null;
    return { kind: "file", path: resolved };
  } catch (error) {
    if (error.code === "MODULE_NOT_FOUND" || error.code === "ERR_PACKAGE_PATH_NOT_EXPORTED") {
      return null;
    }
    throw error;
  }
}

function pluginOperationRoots(pluginRoot) {
  const metadata = readJson(path.join(pluginRoot, "package.json"));
  const candidates = new Set();
  for (const relative of [
    ...(metadata.openclaw?.runtimeExtensions || []),
    metadata.openclaw?.runtimeSetupEntry,
  ]) {
    if (relative) candidates.add(path.resolve(pluginRoot, relative));
  }
  const distRoot = path.join(pluginRoot, "dist");
  for (const candidate of listFiles(
    distRoot,
    (entry) => /\.(?:c|m)?js$/u.test(entry),
  )) {
    const basename = path.basename(candidate);
    if (
      pluginRoot.endsWith(`${path.sep}amazon-bedrock`) ||
      /^(?:api|index|action-runtime\.runtime-|send\.runtime-|monitor-)/u.test(
        basename,
      )
    ) {
      candidates.add(candidate);
    }
  }
  const roots = [...candidates].filter((candidate) => lstatOrNull(candidate)?.isFile());
  if (roots.length === 0) {
    throw new Error(`no operation module roots found for ${pluginRoot}`);
  }
  return roots.toSorted();
}

function computePluginOperationModuleClosure() {
  const roots = PLUGIN_ROOTS.flatMap(pluginOperationRoots);
  const pluginSourcePrefixes = PLUGIN_ROOTS.map(
    (root) => `${fs.realpathSync(path.join(root, "dist"))}${path.sep}`,
  );
  const pending = [...roots];
  const visited = new Set();
  const resolvedEdges = [];
  const unresolved = [];
  const unresolvedComputed = [];

  while (pending.length > 0) {
    const modulePath = pending.pop();
    const realModulePath = fs.realpathSync(modulePath);
    if (visited.has(realModulePath)) continue;
    visited.add(realModulePath);
    const source = fs.readFileSync(modulePath, "utf8");
    const importRecords = parse(source)[0];
    const specifiers = new Set();
    const constantStringSpecifiers = new Map(
      [...source.matchAll(
        /\bconst\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(["'])([^"']+)\2\s*;/gu,
      )].map((match) => [match[1], match[3]]),
    );
    for (const record of importRecords) {
      if (typeof record.n === "string") {
        specifiers.add(record.n);
      } else if (record.d >= 0) {
        const expression = source.slice(record.s, record.e).trim();
        const constantSpecifier = constantStringSpecifiers.get(expression);
        if (constantSpecifier) {
          specifiers.add(constantSpecifier);
        } else {
          unresolvedComputed.push({
            importer: toContainerPath(modulePath),
            expression,
          });
        }
      }
    }
    for (const match of source.matchAll(/\brequire\s*\(([^)]*)\)/gu)) {
      const expression = match[1].trim();
      const literal = expression.match(/^(["'])([^"']+)\1$/u);
      if (literal) {
        specifiers.add(literal[2]);
      } else {
        unresolvedComputed.push({
          importer: toContainerPath(modulePath),
          expression: `require(${expression})`,
        });
      }
    }
    for (const specifier of specifiers) {
      const resolved = resolveModuleFile(modulePath, specifier);
      if (!resolved) {
        unresolved.push({
          importer: toContainerPath(modulePath),
          specifier,
        });
        continue;
      }
      resolvedEdges.push({
        importer: toContainerPath(modulePath),
        specifier,
        kind: resolved.kind,
        resolved: toContainerPath(resolved.path),
      });
      if (
        resolved.kind === "file" &&
        pluginSourcePrefixes.some((prefix) =>
          fs.realpathSync(resolved.path).startsWith(prefix),
        ) &&
        /\.(?:c|m)?js$/u.test(resolved.path)
      ) {
        pending.push(resolved.path);
      }
    }
  }

  if (unresolved.length > 0 || unresolvedComputed.length > 0) {
    throw new Error(
      `Slack/Bedrock operation module closure is unresolved: ${JSON.stringify({
        unresolved,
        unresolvedComputed,
      })}`,
    );
  }
  const modules = [...visited]
    .map((candidate) => ({
      path: toContainerPath(candidate),
      sha256: sha256File(candidate),
    }))
    .toSorted((left, right) => left.path.localeCompare(right.path));
  resolvedEdges.sort(
    (left, right) =>
      left.importer.localeCompare(right.importer) ||
      left.specifier.localeCompare(right.specifier),
  );
  return {
    roots: roots.map(toContainerPath).toSorted(),
    modules,
    resolvedEdges,
    unresolved: [],
    unresolvedComputed: [],
  };
}

function collectReachable(dependencies, roots) {
  const reachable = new Set();
  const pending = [...roots];
  while (pending.length > 0) {
    const current = pending.pop();
    if (reachable.has(current)) continue;
    reachable.add(current);
    pending.push(...(dependencies.get(current) || []));
  }
  return reachable;
}

function controlUiModuleRoots(moduleSet) {
  const html = fs.readFileSync(CONTROL_UI_INDEX, "utf8");
  const roots = [];
  for (const tag of html.match(/<script\b[^>]*>/giu) || []) {
    if (!/\btype\s*=\s*["']module["']/iu.test(tag)) continue;
    const source = tag.match(/\bsrc\s*=\s*["']([^"']+)["']/iu)?.[1];
    if (!source) throw new Error("Control UI module script has no src");
    if (/^(?:[a-z]+:)?\/\//iu.test(source)) {
      throw new Error(`external Control UI module root is forbidden: ${source}`);
    }
    const cleanSource = source.replace(/[?#].*$/u, "");
    const candidate = source.startsWith("/")
      ? path.join(CONTROL_UI_ROOT, cleanSource.replace(/^\/+/u, ""))
      : path.resolve(path.dirname(CONTROL_UI_INDEX), cleanSource);
    if (
      candidate !== CONTROL_UI_ROOT &&
      !candidate.startsWith(`${CONTROL_UI_ROOT}${path.sep}`)
    ) {
      throw new Error(`Control UI module root escapes its asset tree: ${source}`);
    }
    if (!moduleSet.has(candidate)) {
      throw new Error(`Control UI module root is missing: ${source}`);
    }
    roots.push(candidate);
  }
  if (roots.length === 0) {
    throw new Error("expected at least one Control UI module root");
  }
  return [...new Set(roots)];
}

function collectModuleGraph() {
  const distFiles = listFiles(DIST_ROOT);
  const distFileSet = new Set(distFiles);
  const moduleFiles = listFiles(
    DIST_ROOT,
    (candidate) => /\.(?:c|m)?js$/u.test(candidate),
  );
  const moduleSet = new Set(moduleFiles);
  const dependencies = new Map();
  const unresolvedImports = new Map();

  for (const modulePath of moduleFiles) {
    const source = fs.readFileSync(modulePath, "utf8");
    const resolvedImports = [];
    const localSpecifiers = new Set(
      parse(source)[0]
        .map((record) => record.n)
        .filter((specifier) => specifier?.startsWith(".")),
    );
    if (modulePath.startsWith(`${CONTROL_UI_ROOT}${path.sep}`)) {
      // Vite's preload dependency map stores local chunks as string literals
      // rather than ESM import records. They are still fetched at runtime and
      // therefore belong to the Control UI asset closure.
      for (const match of source.matchAll(
        /["'`](\.\/[^"'`?#]+?\.(?:c|m)?js(?:[?#][^"'`]*)?)["'`]/gu,
      )) {
        localSpecifiers.add(match[1]);
      }
    }
    const missing = [];
    for (const specifier of localSpecifiers) {
      const cleanSpecifier = specifier.replace(/[?#].*$/u, "");
      const unresolved = path.resolve(path.dirname(modulePath), cleanSpecifier);
      const resolved = [
        unresolved,
        `${unresolved}.js`,
        `${unresolved}.mjs`,
        `${unresolved}.cjs`,
        path.join(unresolved, "index.js"),
      ].find((candidate) => distFileSet.has(candidate));
      if (!resolved) {
        missing.push(specifier);
      } else if (moduleSet.has(resolved)) {
        resolvedImports.push(resolved);
      }
    }
    dependencies.set(modulePath, [...new Set(resolvedImports)]);
    unresolvedImports.set(modulePath, missing.toSorted());
  }

  const runtimeRoots = ["entry.js", "index.js"]
    .map((name) => path.join(DIST_ROOT, name))
    .filter((candidate) => moduleSet.has(candidate));
  if (runtimeRoots.length !== 2) {
    throw new Error("expected both dist/entry.js and dist/index.js");
  }
  const controlUiRoots = controlUiModuleRoots(moduleSet);
  const runtimeReachable = collectReachable(dependencies, runtimeRoots);
  const controlUiReachable = collectReachable(dependencies, controlUiRoots);
  const reachable = new Set([...runtimeReachable, ...controlUiReachable]);
  const reachableUnresolvedImports = [];
  for (const modulePath of reachable) {
    for (const specifier of unresolvedImports.get(modulePath) || []) {
      reachableUnresolvedImports.push({
        importer: toContainerPath(modulePath),
        specifier,
      });
    }
  }
  return {
    moduleFiles,
    dependencies,
    unresolvedImports,
    reachable,
    reachableUnresolvedImports,
    runtimeReachable,
    runtimeRoots,
    controlUiReachable,
    controlUiRoots,
  };
}

function packageNameFromSpecifier(specifier) {
  if (specifier.startsWith("@")) {
    return specifier.split("/").slice(0, 2).join("/");
  }
  return specifier.split("/", 1)[0];
}

function collectReachableBareImportContract(graph, pluginOperationClosure) {
  const literalBareImports = [];
  const computedDynamicImports = [];
  for (const modulePath of graph.reachable) {
    const source = fs.readFileSync(modulePath, "utf8");
    for (const record of parse(source)[0]) {
      if (typeof record.n !== "string") {
        if (record.d >= 0) {
          computedDynamicImports.push({
            importer: toContainerPath(modulePath),
            expression: source.slice(record.s, record.e),
            coverage:
              "gateway/config/plugins/browser/CLI actual-image smokes after pruning",
          });
        }
        continue;
      }
      const specifier = record.n;
      if (
        specifier.startsWith(".") ||
        specifier.startsWith("/") ||
        specifier.startsWith("node:") ||
        specifier.startsWith("data:")
      ) {
        continue;
      }
      const resolved = resolveModuleFile(modulePath, specifier);
      if (!resolved) {
        throw new Error(
          `reachable bare import is unresolved before pruning: ${JSON.stringify({
            importer: toContainerPath(modulePath),
            specifier,
          })}`,
        );
      }
      literalBareImports.push({
        importer: toContainerPath(modulePath),
        specifier,
        packageName: packageNameFromSpecifier(specifier),
        resolved: toContainerPath(resolved.path),
      });
    }
  }
  for (const edge of pluginOperationClosure.resolvedEdges) {
    if (
      edge.kind === "file" &&
      !edge.specifier.startsWith(".") &&
      !edge.specifier.startsWith("/") &&
      !edge.specifier.startsWith("node:")
    ) {
      literalBareImports.push({
        importer: edge.importer,
        specifier: edge.specifier,
        packageName: packageNameFromSpecifier(edge.specifier),
        resolved: edge.resolved,
      });
    }
  }
  literalBareImports.sort(
    (left, right) =>
      left.importer.localeCompare(right.importer) ||
      left.specifier.localeCompare(right.specifier),
  );
  computedDynamicImports.sort((left, right) =>
    left.importer.localeCompare(right.importer),
  );
  const reachableForbiddenPackageNames = new Set(
    literalBareImports
      .map((entry) => entry.packageName)
      .filter(isForbiddenPackageName),
  );
  if (reachableForbiddenPackageNames.size > 0) {
    throw new Error(
      `forbidden package is reachable through a literal bare import: ${JSON.stringify(
        [...reachableForbiddenPackageNames].toSorted(),
      )}`,
    );
  }
  return {
    literalBareImports,
    computedDynamicImports,
    reachableForbiddenPackageNames,
  };
}

const browserNamePattern = /(?:browser|playwright|chrome-mcp)/iu;
const browserImplementationSignalPatterns = new Map([
  ["browserExtensionSource", /\/\/#region extensions\/browser\//u],
  ["browserPluginRegistration", /function registerBrowserPlugin\(/u],
  ["browserCliRegistration", /registerBrowserCli\(program/u],
  ["browserService", /createBrowserPluginService\(/u],
  [
    "playwrightImport",
    /(?:from\s*|import\s*\(\s*|require\(\s*)["'][^"']*(?:playwright|pw-ai)[^"']*["']/iu,
  ],
  [
    "chromeMcpImport",
    /(?:from\s*|import\s*\(\s*|require\(\s*)["'][^"']*chrome-mcp[^"']*["']/iu,
  ],
]);

function browserImplementationSignals(source) {
  return [...browserImplementationSignalPatterns.entries()]
    .filter(([, pattern]) => pattern.test(source))
    .map(([name]) => name);
}

function sha256File(candidate) {
  const digest = createHash("sha256");
  digest.update(fs.readFileSync(candidate));
  return digest.digest("hex");
}

function collectControlUiFullAssetClosure() {
  const assets = [];
  const walk = (candidate) => {
    const stat = lstatOrNull(candidate);
    if (!stat) return;
    if (stat.isSymbolicLink()) {
      throw new Error(`Control UI asset symlinks are forbidden: ${candidate}`);
    }
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(candidate)) {
        walk(path.join(candidate, entry));
      }
      return;
    }
    if (!stat.isFile()) {
      throw new Error(`unsupported Control UI filesystem object: ${candidate}`);
    }
    const relative = path
      .relative(CONTROL_UI_ROOT, candidate)
      .split(path.sep)
      .join("/");
    if (!relative || relative.startsWith("../")) {
      throw new Error(`Control UI asset escaped its root: ${candidate}`);
    }
    const content = fs.readFileSync(candidate);
    const sourceSha256 = createHash("sha256").update(content).digest("hex");
    let servedContent = content;
    let httpTransform = "identity";
    if (relative === "index.html") {
      const html = content.toString("utf8");
      if (
        html.split("<html").length - 1 !== 1 ||
        html.includes("data-openclaw-terminal-enabled")
      ) {
        throw new Error(
          "Control UI root does not match the reviewed terminal attribute injection contract",
        );
      }
      servedContent = Buffer.from(
        html.replace(
          "<html",
          '<html data-openclaw-terminal-enabled="false"',
        ),
        "utf8",
      );
      httpTransform =
        'insert data-openclaw-terminal-enabled="false" after <html';
    }
    assets.push({
      path: toContainerPath(candidate),
      httpPath: relative === "index.html" ? "/" : `/${relative}`,
      size: stat.size,
      sha256: sourceSha256,
      servedSize: servedContent.length,
      servedSha256: createHash("sha256")
        .update(servedContent)
        .digest("hex"),
      httpTransform,
    });
  };
  walk(CONTROL_UI_ROOT);
  assets.sort((left, right) => left.path.localeCompare(right.path));
  if (!assets.some((asset) => asset.httpPath === "/")) {
    throw new Error("Control UI full asset closure has no root index");
  }
  const httpPaths = assets.map((asset) => asset.httpPath);
  if (httpPaths.length !== new Set(httpPaths).size) {
    throw new Error("Control UI full asset closure has duplicate HTTP paths");
  }
  return assets;
}

function disableJitiExtensionSourceTransformLoader() {
  const exactLoader = [
    "async function loadCreateJitiLoaderFactory() {",
    "\tif (createJitiLoaderFactory) return createJitiLoaderFactory;",
    '\tconst loaded = await import("jiti/static");',
    '\tif (typeof loaded.createJiti !== "function") throw new Error("jiti/static module did not export createJiti");',
    "\tcreateJitiLoaderFactory = loaded.createJiti;",
    "\treturn createJitiLoaderFactory;",
    "}",
  ].join("\n");
  const failClosedLoader = [
    "async function loadCreateJitiLoaderFactory() {",
    '\tthrow new Error("OpenClaw extension source transforms are disabled in the TeamAgent runtime");',
    "}",
  ].join("\n");
  const matches = [];
  for (const candidate of listFiles(
    DIST_ROOT,
    (entry) => /\.(?:c|m)?js$/u.test(entry),
  )) {
    const source = fs.readFileSync(candidate, "utf8");
    const count = source.split(exactLoader).length - 1;
    if (count > 0) matches.push({ candidate, source, count });
  }
  if (
    matches.length !== 1 ||
    matches[0].count !== 1 ||
    matches[0].source.split('"jiti/static"').length - 1 !== 1
  ) {
    throw new Error(
      `expected exactly one reviewed jiti source-transform loader: ${JSON.stringify(
        matches.map((match) => ({
          path: toContainerPath(match.candidate),
          loaderCount: match.count,
          jitiStaticLiteralCount:
            match.source.split('"jiti/static"').length - 1,
        })),
      )}`,
    );
  }
  const [{ candidate, source }] = matches;
  const originalSha256 = createHash("sha256").update(source).digest("hex");
  const patched = source.replace(exactLoader, failClosedLoader);
  if (
    patched === source ||
    patched.includes('"jiti/static"') ||
    !patched.includes(
      "OpenClaw extension source transforms are disabled in the TeamAgent runtime",
    )
  ) {
    throw new Error("jiti source-transform loader fail-closed rewrite failed");
  }
  fs.writeFileSync(candidate, patched);
  return {
    path: toContainerPath(candidate),
    originalSha256,
    patchedSha256: sha256File(candidate),
    removedBareImport: "jiti/static",
    sourceTransformLoaderFailClosed: true,
    nativeJavaScriptExtensionLoaderRetained: true,
  };
}

function disableTypeScriptCodeModeCompiler() {
  const exactLoader =
    'const typescriptRuntimeLoader = createLazyPromiseLoader(() => import("typescript"), { cacheRejections: true });';
  const failClosedLoader =
    'const typescriptRuntimeLoader = createLazyPromiseLoader(() => Promise.reject(new Error("TypeScript code mode is disabled in the TeamAgent runtime")), { cacheRejections: true });';
  const exactLanguages = [
    "function readLanguages(value) {",
    '\tif (!Array.isArray(value)) return ["javascript", "typescript"];',
    '\tconst languages = value.filter((entry) => entry === "javascript" || entry === "typescript");',
    '\treturn languages.length > 0 ? uniqueValues(languages) : ["javascript", "typescript"];',
    "}",
  ].join("\n");
  const javascriptOnlyLanguages = [
    "function readLanguages(value) {",
    '\tif (!Array.isArray(value)) return ["javascript"];',
    '\tconst languages = value.filter((entry) => entry === "javascript");',
    '\treturn languages.length > 0 ? uniqueValues(languages) : ["javascript"];',
    "}",
  ].join("\n");
  const matches = [];
  for (const candidate of listFiles(
    DIST_ROOT,
    (entry) => /\.(?:c|m)?js$/u.test(entry),
  )) {
    const source = fs.readFileSync(candidate, "utf8");
    const loaderCount = source.split(exactLoader).length - 1;
    const languagesCount = source.split(exactLanguages).length - 1;
    if (loaderCount > 0 || languagesCount > 0) {
      matches.push({ candidate, source, loaderCount, languagesCount });
    }
  }
  if (
    matches.length !== 1 ||
    matches[0].loaderCount !== 1 ||
    matches[0].languagesCount !== 1
  ) {
    throw new Error(
      `expected exactly one reviewed TypeScript code-mode loader: ${JSON.stringify(
        matches.map((match) => ({
          path: toContainerPath(match.candidate),
          loaderCount: match.loaderCount,
          languagesCount: match.languagesCount,
        })),
      )}`,
    );
  }
  const [{ candidate, source }] = matches;
  const originalSha256 = createHash("sha256").update(source).digest("hex");
  const patched = source
    .replace(exactLoader, failClosedLoader)
    .replace(exactLanguages, javascriptOnlyLanguages);
  if (
    patched === source ||
    patched.includes('import("typescript")') ||
    !patched.includes(
      "TypeScript code mode is disabled in the TeamAgent runtime",
    )
  ) {
    throw new Error("TypeScript code-mode fail-closed rewrite failed");
  }
  fs.writeFileSync(candidate, patched);
  return {
    path: toContainerPath(candidate),
    originalSha256,
    patchedSha256: sha256File(candidate),
    removedBareImport: "typescript",
    compilerLoaderFailClosed: true,
    advertisedLanguages: ["javascript"],
  };
}

// The upstream sessions bundle has one optional source-transform branch that
// dynamically imports jiti. TeamAgent does not permit runtime TypeScript,
// CommonJS source transforms, or user-supplied extension source. Verify the
// exact reviewed upstream implementation, replace only that branch with a
// deterministic fail-closed facade, and then compute every dependency/module
// closure before deleting packages or rewriting package metadata.
const jitiExtensionSourceTransformFacade =
  disableJitiExtensionSourceTransformLoader();
const typeScriptCodeModeCompilerFacade = disableTypeScriptCodeModeCompiler();
const prePruneGraph = collectModuleGraph();
// Prove package and real Slack/Bedrock operation closures against the
// unmodified upstream metadata and module tree.  Nothing is deleted and no
// package.json is rewritten before these contracts pass.  Declared browser/
// compiler packages may be excluded only when no reachable literal import uses
// them; the post-prune operation and gateway smokes cover computed runtime use.
const prePrunePluginOperationClosure = computePluginOperationModuleClosure();
const prePruneBareImportContract = collectReachableBareImportContract(
  prePruneGraph,
  prePrunePluginOperationClosure,
);
const productionPackageClosure = computeProductionPackageClosure(
  prePruneBareImportContract.reachableForbiddenPackageNames,
);
const reachableBrowserImplementations = [];
for (const modulePath of prePruneGraph.reachable) {
  const signals = browserImplementationSignals(
    fs.readFileSync(modulePath, "utf8"),
  );
  if (signals.length > 0) {
    reachableBrowserImplementations.push({
      path: toContainerPath(modulePath),
      signals,
    });
  }
}
if (reachableBrowserImplementations.length > 0) {
  throw new Error(
    `browser implementation is reachable from a runtime root: ${JSON.stringify(
      reachableBrowserImplementations,
    )}`,
  );
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

const removedBrowserSet = new Set(removedBrowserChunks);
const removedBrowserDependentChunks = [];
for (const modulePath of initialGraph.moduleFiles) {
  const containerPath = toContainerPath(modulePath);
  if (
    removedBrowserSet.has(containerPath) ||
    initialGraph.reachable.has(modulePath)
  ) {
    continue;
  }
  const importsRemovedBrowserChunk = (
    initialGraph.dependencies.get(modulePath) || []
  ).some((dependency) =>
    removedBrowserSet.has(toContainerPath(dependency)),
  );
  if (!importsRemovedBrowserChunk) continue;
  const source = fs.readFileSync(modulePath, "utf8");
  const isEmptySideEffectImportStub =
    /^(?:\s*import\s*["'][^"']+["'];?)+\s*export\s*\{\s*\};?\s*$/u.test(
      source,
    );
  if (!isEmptySideEffectImportStub) continue;
  fs.rmSync(modulePath, { force: true });
  removedBrowserSet.add(containerPath);
  removedBrowserDependentChunks.push(containerPath);
}
removedBrowserDependentChunks.sort();

const cliMetadataPath = path.join(DIST_ROOT, "cli-startup-metadata.json");
const cliMetadata = readJson(cliMetadataPath);
delete cliMetadata.browserHelpSourceSignature;
delete cliMetadata.browserHelpText;
writeJson(cliMetadataPath, cliMetadata);

const finalGraph = collectModuleGraph();
const residualDeadBrowserChunks = [];
const browserRegistrationChunks = [];
const sharedBrowserChunks = [];
const preservedControlUiBrowserChunks = [];
const retainedBrowserFacadeChunks = [];
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
    if (
      source.includes("startBrowserBridgeServer") ||
      source.includes("stopBrowserBridgeServer")
    ) {
      retainedBrowserFacadeChunks.push({
        path: toContainerPath(modulePath),
        sha256: sha256File(modulePath),
        publicFacade:
          source.includes("async function startBrowserBridgeServer(") &&
          source.includes("loadFacadeModule().startBrowserBridgeServer"),
        genericChildProcessPrimitives:
          source.includes('from "node:child_process"') &&
          /\bspawn\s*\(/u.test(source),
        implementationSignals: browserImplementationSignals(source),
      });
    }
    if (finalGraph.controlUiReachable.has(modulePath)) {
      preservedControlUiBrowserChunks.push({
        path: toContainerPath(modulePath),
        sha256: sha256File(modulePath),
        implementationSignals: browserImplementationSignals(source),
      });
    }
  }
  if (
    finalGraph.reachable.has(modulePath) &&
    browserImplementationSignals(source).length > 0
  ) {
    browserRegistrationChunks.push(toContainerPath(modulePath));
  }
  if (source.includes("extensions/browser/")) {
    const allowedSidecarMarker =
      finalGraph.reachable.has(modulePath) &&
      source.includes("//#region scripts/lib/bundled-runtime-sidecar-paths.json") &&
      source.includes('"dist/extensions/browser/runtime-api.js"') &&
      browserImplementationSignals(source).length === 0;
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
  residualBrowserSidecarMarkers.length > 0 ||
    finalGraph.reachableUnresolvedImports.length > 0 ||
    preservedControlUiBrowserChunks.length === 0 ||
    retainedBrowserFacadeChunks.length !== 1 ||
    retainedBrowserFacadeChunks.some(
      (candidate) =>
        candidate.publicFacade !== true ||
        candidate.implementationSignals.length > 0,
    ) ||
    preservedControlUiBrowserChunks.some(
    (candidate) => candidate.implementationSignals.length > 0,
  )
) {
  throw new Error(
    `browser reachability contract failed: ${JSON.stringify({
      residualDeadBrowserChunks,
      browserRegistrationChunks,
      residualBrowserSidecarMarkers,
      reachableUnresolvedImports: finalGraph.reachableUnresolvedImports,
      preservedControlUiBrowserChunks,
      retainedBrowserFacadeChunks,
    })}`,
  );
}

const isForbidden = (name) =>
  isForbiddenPackageName(name);

const forbiddenPackagesRemoved = [];
function normalizePackageMetadata(packagePath, metadata, keepBin = false) {
  delete metadata.gitHead;
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

const markedPackageRealPaths = productionPackageClosure.markedRealPaths;

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

const postPrunePluginOperationClosure = computePluginOperationModuleClosure();
if (
  JSON.stringify(postPrunePluginOperationClosure.roots) !==
    JSON.stringify(prePrunePluginOperationClosure.roots) ||
  JSON.stringify(postPrunePluginOperationClosure.modules) !==
    JSON.stringify(prePrunePluginOperationClosure.modules) ||
  JSON.stringify(postPrunePluginOperationClosure.resolvedEdges) !==
    JSON.stringify(prePrunePluginOperationClosure.resolvedEdges)
) {
  throw new Error(
    "Slack/Bedrock operation module closure changed during pruning",
  );
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

// This must run after every deletion pass.  Otherwise the retained report can
// describe an asset that a later development-payload cleanup removed.
const verifiedFinalGraph = collectModuleGraph();
const sortedContainerPaths = (entries) =>
  [...entries].map(toContainerPath).toSorted();
if (
  JSON.stringify(sortedContainerPaths(verifiedFinalGraph.runtimeReachable)) !==
    JSON.stringify(sortedContainerPaths(finalGraph.runtimeReachable)) ||
  JSON.stringify(sortedContainerPaths(verifiedFinalGraph.controlUiReachable)) !==
    JSON.stringify(sortedContainerPaths(finalGraph.controlUiReachable)) ||
  verifiedFinalGraph.reachableUnresolvedImports.length > 0
) {
  throw new Error(
    `reachable module closure changed during final cleanup: ${JSON.stringify({
      beforeRuntime: sortedContainerPaths(finalGraph.runtimeReachable),
      afterRuntime: sortedContainerPaths(verifiedFinalGraph.runtimeReachable),
      beforeControlUi: sortedContainerPaths(finalGraph.controlUiReachable),
      afterControlUi: sortedContainerPaths(verifiedFinalGraph.controlUiReachable),
      unresolved: verifiedFinalGraph.reachableUnresolvedImports,
    })}`,
  );
}
const controlUiFullAssets = collectControlUiFullAssetClosure();
const controlUiFullAssetPaths = new Set(
  controlUiFullAssets.map((asset) => asset.path),
);
for (const modulePath of verifiedFinalGraph.controlUiReachable) {
  if (!controlUiFullAssetPaths.has(toContainerPath(modulePath))) {
    throw new Error(
      `Control UI module closure is outside final full served asset closure: ${modulePath}`,
    );
  }
}

writeJson(REPORT_PATH, {
  schemaVersion: 2,
  browser: {
    graphRoots: verifiedFinalGraph.runtimeRoots.map(toContainerPath).toSorted(),
    totalModuleCount: verifiedFinalGraph.moduleFiles.length,
    reachableModuleCount: verifiedFinalGraph.runtimeReachable.size,
    removedImplementationChunks: removedBrowserChunks,
    removedDependentChunks: removedBrowserDependentChunks,
    residualUnreachableBrowserCandidates: 0,
    reachableRegistrationChunks: 0,
    sharedReachableChunks: sharedBrowserChunks.toSorted(),
    reachableBrowserNamedPayloadCount: sharedBrowserChunks.length,
    reachableBrowserPayloadZero: false,
    reachableBrowserImplementationModules: 0,
    browserCliCommandRegistered: false,
    genericOpenClawCliRetained: true,
    browserExecutableOrPlaywrightPresent: false,
    usableBrowserControlPath: false,
    retainedFailClosedFacade: retainedBrowserFacadeChunks,
    controlUiGraphRoots: verifiedFinalGraph.controlUiRoots
      .map(toContainerPath)
      .toSorted(),
    controlUiReachableModuleCount: verifiedFinalGraph.controlUiReachable.size,
    controlUiReachableChunks: [...verifiedFinalGraph.controlUiReachable]
      .map(toContainerPath)
      .toSorted(),
    controlUiReachableModuleAssets: [...verifiedFinalGraph.controlUiReachable]
      .map((candidate) => ({
        path: toContainerPath(candidate),
        sha256: sha256File(candidate),
      }))
      .toSorted((left, right) => left.path.localeCompare(right.path)),
    controlUiServedAssetCount: controlUiFullAssets.length,
    controlUiServedAssets: controlUiFullAssets,
    controlUiFullAssetClosurePolicy:
      "hash every on-disk regular file and its deterministic HTTP representation under dist/control-ui",
    controlUiDynamicAssetRegistrationsCoveredByWholeTree: true,
    controlUiBootstrapConfigRuntimeContractRequired: true,
    controlUiMissingLocalImports: 0,
    preservedControlUiBrowserChunks: preservedControlUiBrowserChunks.toSorted(
      (left, right) => left.path.localeCompare(right.path),
    ),
    sidecarPathMarkersValidatedAsDataOnly: 1,
    cliHelpMetadataRemoved: true,
  },
  pluginOperations: {
    closureComputedBeforeMetadataRewrite: true,
    roots: postPrunePluginOperationClosure.roots,
    moduleCount: postPrunePluginOperationClosure.modules.length,
    modules: postPrunePluginOperationClosure.modules,
    resolvedEdgeCount: postPrunePluginOperationClosure.resolvedEdges.length,
    resolvedEdges: postPrunePluginOperationClosure.resolvedEdges,
    unresolvedImports: [],
    unresolvedComputedImports: [],
    postPruneClosureExactMatch: true,
  },
  packages: {
    closureComputedBeforeMetadataRewrite: true,
    jitiExtensionSourceTransformFacade,
    typeScriptCodeModeCompilerFacade,
    reachableLiteralBareImports:
      prePruneBareImportContract.literalBareImports,
    reachableComputedDynamicImports:
      prePruneBareImportContract.computedDynamicImports,
    excludedForbiddenDeclarations:
      productionPackageClosure.excludedForbiddenDeclarations,
    prePruneProductionClosure: productionPackageClosure.retained,
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
