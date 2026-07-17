def fail($message): error("OpenClaw task definition: " + $message);

def exactly_one_openclaw:
  [.containerDefinitions[] | select(.name == "openclaw")] |
  if length == 1 then .[0] else fail("expected exactly one openclaw container") end;

def required_secret_names:
  [
    "TEAMAGENT_MCP_BEARER",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "OPENCLAW_GATEWAY_TOKEN"
  ];

def harden_openclaw_container($image):
  .image = $image |
  .essential = true |
  .readonlyRootFilesystem = true |
  .user = "65532:65532" |
  .privileged = false |
  del(.entryPoint, .command, .dockerSecurityOptions) |
  .linuxParameters = (
    (.linuxParameters // {}) |
    del(.tmpfs) |
    .capabilities = {drop: ["ALL"]}
  ) |
  .environment = [
    (.environment // [])[] |
    select(.name != "OPENCLAW_CONFIG_PATH")
  ] |
  .mountPoints = (
    [
      (.mountPoints // [])[] |
      select(.containerPath != "/tmp" and .sourceVolume != "openclaw-tmp")
    ] +
    [{
      sourceVolume: "openclaw-tmp",
      containerPath: "/tmp",
      readOnly: false
    }]
  ) |
  .stopTimeout = 30 |
  .healthCheck = {
    command: [
      "CMD",
      "node",
      "-e",
      "fetch(\"http://127.0.0.1:18789/readyz\").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
    ],
    interval: 30,
    timeout: 5,
    retries: 5,
    startPeriod: 40
  };

(.taskDefinition // .) |
if ($image | test("@sha256:[0-9a-f]{64}$")) then . else
  fail("release manifest image must be digest-addressed")
end |
exactly_one_openclaw as $before |
if (
  [
    $before.environment[]? |
    select(.name as $name | required_secret_names | index($name))
  ] | length
) == 0 then . else
  fail("required secrets must not be plaintext environment entries")
end |
if (
  required_secret_names -
  [$before.secrets[]?.name]
) | length == 0 then . else
  fail("one or more required Secrets Manager bindings are missing")
end |
{
  family,
  taskRoleArn,
  executionRoleArn,
  networkMode,
  containerDefinitions,
  volumes,
  placementConstraints,
  requiresCompatibilities,
  cpu,
  memory,
  ipcMode,
  pidMode,
  proxyConfiguration,
  inferenceAccelerators,
  ephemeralStorage,
  runtimePlatform,
  enableFaultInjection
} |
with_entries(select(.value != null)) |
.networkMode = "awsvpc" |
.requiresCompatibilities = ["FARGATE"] |
.runtimePlatform = {
  cpuArchitecture: "ARM64",
  operatingSystemFamily: "LINUX"
} |
.volumes = (
  [
    (.volumes // [])[] |
    select(.name != "openclaw-tmp")
  ] +
  [{name: "openclaw-tmp"}]
) |
.containerDefinitions |= map(
  if .name == "openclaw" then harden_openclaw_container($image) else . end
) |
exactly_one_openclaw as $after |
if (
  $after.readonlyRootFilesystem == true and
  $after.user == "65532:65532" and
  $after.privileged == false and
  $after.linuxParameters.capabilities.drop == ["ALL"] and
  ($after.linuxParameters | has("tmpfs") | not) and
  ($after | has("dockerSecurityOptions") | not) and
  ($after | has("entryPoint") | not) and
  ($after | has("command") | not) and
  ([
    $after.mountPoints[] |
    select(
      .sourceVolume == "openclaw-tmp" and
      .containerPath == "/tmp" and
      .readOnly == false
    )
  ] | length) == 1 and
  ($after.healthCheck.command[3] | contains("/readyz"))
) then . else
  fail("rendered hardening contract is incomplete")
end
