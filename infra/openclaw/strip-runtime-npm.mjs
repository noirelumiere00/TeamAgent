// Remove the npm CLI (Wolfi package npm-12) and node-gyp (which only npm-12
// depends on) from a copied Chainguard node root filesystem, as
// `apk del npm-12 node-gyp` would, without running apk or a shell in the
// runtime image.
//
// Why: OpenClaw never executes npm, but the ECR (Inspector) gate scans the
// final image and blocked releases on CVEs in npm's bundled dependencies
// (2026-09-16 #424, 2026-10-01 #503). Removing the package is the fix the
// team requires for HIGH findings, instead of a time-boxed exception.
//
// The apk installed database is the source of truth for which files belong
// to the two packages; only those files are deleted, and their entries are
// dropped from the database so OS-package scanners no longer report them.
// The build fails if anything still depends on the removed packages or if a
// listed file is missing (an unexpected base layout).
//
// Usage: node strip-runtime-npm.mjs <rootfs>

import fs from "node:fs";
import path from "node:path";

const REMOVED = ["npm-12", "node-gyp"];
const root = process.argv[2];
if (!root || !fs.statSync(root).isDirectory()) {
  throw new Error("usage: node strip-runtime-npm.mjs <rootfs>");
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
// Deepest first; keep directories another package still owns or that are not empty.
for (const dir of [...removedDirs].sort((a, b) => b.length - a.length)) {
  if (keptDirs.has(dir)) continue;
  const full = path.join(root, dir);
  if (fs.existsSync(full) && fs.readdirSync(full).length === 0) fs.rmdirSync(full);
}
fs.writeFileSync(dbPath, `${kept.join("\n\n")}\n\n`);

for (const leftover of [
  "usr/lib/node_modules/npm",
  "usr/lib/node_modules/node-gyp",
  "usr/bin/npm",
  "usr/bin/npx",
  "usr/bin/node-gyp",
]) {
  let present = true;
  try {
    fs.lstatSync(path.join(root, leftover)); // lstat: a dangling symlink also counts
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    present = false;
  }
  if (present) throw new Error(`still present: /${leftover}`);
}
console.log(
  JSON.stringify({
    removedPackages: removed.map((block) => `${nameOf(block)}-${field(block, "V")[0]}`),
    removedFiles: removedFiles.length,
    keptPackages: kept.length,
  }),
);
