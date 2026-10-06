// Remove packages the OpenClaw runtime never uses from a copied Chainguard node
// root filesystem, as `apk del` would, without running apk or a shell in the
// runtime image:
//   npm-12, node-gyp  the npm CLI and node-gyp (only npm-12 depends on it). OpenClaw
//                     never executes npm, but the ECR (Inspector) gate blocked
//                     releases on CVEs in npm's bundled dependencies
//                     (2026-09-16 #424, 2026-10-01 #503).
//   busybox           /bin/sh and 218 other applets. The gateway never needs a
//                     shell (exec is denied in openclaw.config.json5, the
//                     entrypoint refuses shell commands, the ECS health check
//                     is CMD /usr/bin/node, ECS Exec is off), and a shell in the
//                     image is the first thing an attacker who reaches code
//                     execution would use. OpenClaw's Linux helpers that look
//                     for /bin/sh (child oom_score wrapping, login-shell env)
//                     check for it and fall back when it is absent.
//
// The apk installed database is the source of truth for which files belong
// to each package; only those files are deleted, and their entries are
// dropped from the database so OS-package scanners no longer report them.
// busybox's applet symlinks are not package files (an apk trigger creates
// them from etc/busybox-paths.d/busybox), so that list is cross-checked
// against every symlink that actually points at busybox before both are
// removed. The build fails if anything still depends on a removed package,
// if the database and the filesystem disagree, or if the removal leaves any
// symlink dangling. Symlinks are never followed here: absolute targets would resolve
// against the builder's root, not <rootfs>.
//
// Usage: node strip-runtime-packages.mjs <rootfs>

import fs from "node:fs";
import path from "node:path";

const REMOVED = ["npm-12", "node-gyp", "busybox"];
const BUSYBOX_APPLET_LIST = "etc/busybox-paths.d/busybox";
const BUSYBOX_TARGET = "/bin/busybox";
const SKIP_SCAN = new Set(["proc", "sys", "dev"]);

const root = process.argv[2];
if (!root || !fs.statSync(root).isDirectory()) {
  throw new Error("usage: node strip-runtime-packages.mjs <rootfs>");
}
const dbPath = path.join(root, "usr/lib/apk/db/installed");
const raw = fs.readFileSync(dbPath, "utf8");
if (!raw.endsWith("\n\n")) throw new Error("unexpected apk installed db terminator");

const blocks = raw.slice(0, -2).split("\n\n");
const field = (block, key) =>
  block.split("\n").filter((line) => line.startsWith(`${key}:`)).map((line) => line.slice(2));
const nameOf = (block) => {
  const names = field(block, "P");
  if (names.length !== 1) throw new Error("apk db block without exactly one P: line");
  return names[0];
};
const lexists = (full) => {
  try {
    fs.lstatSync(full);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
};

const removed = blocks.filter((block) => REMOVED.includes(nameOf(block)));
const kept = blocks.filter((block) => !REMOVED.includes(nameOf(block)));
if (removed.map(nameOf).sort().join(",") !== [...REMOVED].sort().join(",")) {
  throw new Error(`expected apk packages ${REMOVED.join(", ")}; found ${removed.map(nameOf)}`);
}

// Names and provides of the removed packages (e.g. npm=12.1.0-r2, cmd:npm=...).
const removedProvides = new Set(REMOVED);
for (const block of removed) {
  for (const provides of field(block, "p").join(" ").split(" ").filter(Boolean)) {
    removedProvides.add(provides.split("=")[0]);
  }
}
for (const block of kept) {
  for (const dep of field(block, "D").join(" ").split(" ").filter(Boolean)) {
    const name = dep.replace(/^!/, "").split(/[<>=~]/)[0];
    if (removedProvides.has(name)) {
      throw new Error(`${nameOf(block)} still depends on removed ${name}`);
    }
  }
}

// Every symlink under <rootfs> as [relative path, raw target], without following any.
function symlinks() {
  const found = [];
  const walk = (rel) => {
    for (const entry of fs.readdirSync(path.join(root, rel), { withFileTypes: true })) {
      const child = rel ? `${rel}/${entry.name}` : entry.name;
      if (!rel && SKIP_SCAN.has(entry.name)) continue;
      if (entry.isSymbolicLink()) found.push([child, fs.readlinkSync(path.join(root, child))]);
      else if (entry.isDirectory()) walk(child);
    }
  };
  walk("");
  return found;
}

// busybox applet links: the trigger's list and the real links must be the same set.
const applets = fs
  .readFileSync(path.join(root, BUSYBOX_APPLET_LIST), "utf8")
  .split("\n")
  .map((line) => line.trim().replace(/^\//, ""))
  .filter(Boolean);
const busyboxLinks = symlinks()
  .filter(([, target]) => path.basename(target) === "busybox")
  .map(([rel]) => rel);
const appletSet = new Set(applets);
const linkSet = new Set(busyboxLinks);
const onlyListed = applets.filter((rel) => !linkSet.has(rel));
const onlyLinked = busyboxLinks.filter((rel) => !appletSet.has(rel));
if (applets.length === 0 || onlyListed.length || onlyLinked.length) {
  throw new Error(
    `busybox applet list and links differ: listed-only=${onlyListed.slice(0, 5)} linked-only=${onlyLinked.slice(0, 5)}`,
  );
}
for (const rel of busyboxLinks) {
  if (fs.readlinkSync(path.join(root, rel)) !== BUSYBOX_TARGET) {
    throw new Error(`unexpected busybox link target: /${rel}`);
  }
}

// Resolve a path inside <rootfs> component by component, treating absolute
// symlink targets as relative to <rootfs>. Returns false when it dangles.
function resolvesInRoot(rel) {
  let pending = rel.split("/").filter(Boolean);
  let current = [];
  for (let hops = 0; pending.length; ) {
    const part = pending.shift();
    if (part === ".") continue;
    if (part === "..") {
      current.pop();
      continue;
    }
    const full = path.join(root, ...current, part);
    let stat;
    try {
      stat = fs.lstatSync(full);
    } catch (error) {
      if (error.code === "ENOENT") return false;
      throw error;
    }
    if (!stat.isSymbolicLink()) {
      current.push(part);
      continue;
    }
    if (++hops > 40) return false;
    const target = fs.readlinkSync(full);
    if (target.startsWith("/")) current = [];
    pending = [...target.split("/").filter(Boolean), ...pending];
  }
  return true;
}

// Links that already dangle in the base (e.g. /etc/mtab -> ../proc/self/mounts,
// valid only at runtime) are not ours to judge; only newly dangling links fail.
const danglingBefore = new Set(
  symlinks()
    .filter(([rel]) => !resolvesInRoot(rel))
    .map(([rel]) => rel),
);

const ownedDirs = (block) => new Set(field(block, "F"));
const keptDirs = new Set(kept.flatMap((block) => [...ownedDirs(block)]));
const removedFiles = [];
const removedDirs = new Set();
for (const block of removed) {
  let dir = null;
  for (const line of block.split("\n")) {
    if (line.startsWith("F:")) {
      dir = line.slice(2);
      removedDirs.add(dir);
    } else if (line.startsWith("R:")) {
      if (dir === null) throw new Error(`${nameOf(block)}: R: before any F:`);
      removedFiles.push(path.join(dir, line.slice(2)));
    }
  }
}
for (const file of removedFiles) {
  fs.rmSync(path.join(root, file)); // throws when missing: the layout is not what the db says
}
for (const rel of busyboxLinks) {
  fs.unlinkSync(path.join(root, rel));
}
// Deepest first; keep directories another package still owns or that are not empty.
for (const dir of [...removedDirs].sort((a, b) => b.length - a.length)) {
  if (keptDirs.has(dir)) continue;
  const full = path.join(root, dir);
  if (lexists(full) && fs.readdirSync(full).length === 0) fs.rmdirSync(full);
}
fs.writeFileSync(dbPath, `${kept.join("\n\n")}\n\n`);

for (const leftover of [
  "usr/lib/node_modules/npm",
  "usr/lib/node_modules/node-gyp",
  "usr/bin/npm",
  "usr/bin/npx",
  "usr/bin/node-gyp",
  "usr/bin/busybox",
  "usr/bin/sh",
  BUSYBOX_APPLET_LIST,
]) {
  if (lexists(path.join(root, leftover))) throw new Error(`still present: /${leftover}`);
}
const remainingLinks = symlinks();
const toBusybox = remainingLinks.filter(([, target]) => path.basename(target) === "busybox");
if (toBusybox.length) throw new Error(`symlinks still point at busybox: /${toBusybox[0][0]}`);
const dangling = remainingLinks.filter(([rel]) => !danglingBefore.has(rel) && !resolvesInRoot(rel));
if (dangling.length) throw new Error(`dangling symlinks: ${dangling.slice(0, 5).map(([rel]) => `/${rel}`)}`);

console.log(
  JSON.stringify({
    removedPackages: removed.map((block) => `${nameOf(block)}-${field(block, "V")[0]}`),
    removedFiles: removedFiles.length,
    removedBusyboxApplets: busyboxLinks.length,
    keptPackages: kept.length,
  }),
);
