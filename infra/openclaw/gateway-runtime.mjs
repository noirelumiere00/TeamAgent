#!/usr/bin/env node

// OpenClaw's packaged launcher respawns when NODE_COMPILE_CACHE points at its
// unversioned parent directory. Its signal supervisor starts forcing the child
// down after one second, which can turn an otherwise clean gateway shutdown
// into exit 1. Keep the reviewed production gateway as PID 1 instead: disable
// that optional cache before execing the official launcher in-place.

const expectedArgs = ["gateway", "--bind", "loopback", "--port", "18789"];
const args = process.argv.slice(2);
if (JSON.stringify(args) !== JSON.stringify(expectedArgs)) {
  throw new Error("gateway-runtime.mjs accepts only the canonical gateway command");
}
if (typeof process.execve !== "function") {
  throw new Error("process.execve is required for the gateway PID 1 contract");
}

const gatewayEnv = {
  ...process.env,
  NODE_DISABLE_COMPILE_CACHE: "1",
};
delete gatewayEnv.NODE_COMPILE_CACHE;
delete gatewayEnv.OPENCLAW_PACKAGED_COMPILE_CACHE_RESPAWNED;

process.chdir("/app");
process.execve(
  process.execPath,
  [process.execPath, "/app/openclaw.mjs", ...expectedArgs],
  gatewayEnv,
);
throw new Error("process.execve returned unexpectedly");
