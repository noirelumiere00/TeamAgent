def fail($message): error("OpenClaw task definition: " + $message);

def expected:
  {
    account: "718959508629",
    region: "ap-northeast-1",
    family: "teamagent-dev-openclaw",
    taskRoleArn: "arn:aws:iam::718959508629:role/teamagent-dev-openclaw-task",
    executionRoleArn: "arn:aws:iam::718959508629:role/teamagent-dev-ecs-exec-openclaw",
    repository: "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw",
    logGroup: "/teamagent/dev/openclaw"
  };

def required_secret_names:
  [
    "TEAMAGENT_MCP_BEARER",
    "TEAMAGENT_CALLER_CLAIM_SECRET",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "OPENCLAW_GATEWAY_TOKEN"
  ];

def required_secret_patterns:
  {
    TEAMAGENT_MCP_BEARER:
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/mcp/bearer-[A-Za-z0-9]+$",
    TEAMAGENT_CALLER_CLAIM_SECRET:
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/openclaw/caller-claim-hmac-[A-Za-z0-9]+$",
    SLACK_BOT_TOKEN:
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/openclaw/slack-bot-token-[A-Za-z0-9]+$",
    SLACK_APP_TOKEN:
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/openclaw/slack-app-token-[A-Za-z0-9]+$",
    OPENCLAW_GATEWAY_TOKEN:
      "^arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/openclaw/gateway-token-[A-Za-z0-9]+$"
  };

def valid_volumes:
  type == "array" and
  length == 2 and
  (map(.name) | sort) == ["state", "tmp"] and
  ([.[] | select(. == {name: "tmp"})] | length) == 1 and
  ([.[] | select(
    .name == "state" and
    (keys | sort) == ["efsVolumeConfiguration", "name"] and
    (.efsVolumeConfiguration | keys | sort) ==
      ["authorizationConfig", "fileSystemId", "rootDirectory", "transitEncryption"] and
    (.efsVolumeConfiguration.fileSystemId |
      test("^fs-[0-9a-f]{8,17}$")) and
    .efsVolumeConfiguration.rootDirectory == "/" and
    .efsVolumeConfiguration.transitEncryption == "ENABLED" and
    (.efsVolumeConfiguration.authorizationConfig | keys | sort) ==
      ["accessPointId", "iam"] and
    (.efsVolumeConfiguration.authorizationConfig.accessPointId |
      test("^fsap-[0-9a-f]{8,17}$")) and
    .efsVolumeConfiguration.authorizationConfig.iam == "ENABLED"
  )] | length) == 1;

def exact_mounts:
  [
    {
      sourceVolume: "tmp",
      containerPath: "/tmp",
      readOnly: false
    },
    {
      sourceVolume: "state",
      containerPath: "/tmp/teamagent-openclaw/state",
      readOnly: false
    }
  ];

def allowed_current_task_keys:
  [
    "taskDefinitionArn",
    "containerDefinitions",
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "revision",
    "volumes",
    "status",
    "requiresAttributes",
    "placementConstraints",
    "compatibilities",
    "runtimePlatform",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "registeredAt",
    "registeredBy",
    "deregisteredAt",
    "enableFaultInjection"
  ];

def allowed_container_keys:
  [
    "name",
    "image",
    "essential",
    "readonlyRootFilesystem",
    "user",
    "privileged",
    "entryPoint",
    "command",
    "dockerSecurityOptions",
    "linuxParameters",
    "environment",
    "secrets",
    "logConfiguration",
    "mountPoints",
    "stopTimeout",
    "healthCheck"
  ];

def exactly_one_openclaw:
  if (.containerDefinitions | type) == "array" and
     (.containerDefinitions | length) == 1 and
     .containerDefinitions[0].name == "openclaw"
  then .containerDefinitions[0]
  else fail("expected exactly one container named openclaw; sidecars are forbidden")
  end;

def assert_unique_names($entries; $label):
  ($entries | map(.name)) as $names |
  if ($names | length) == ($names | unique | length)
  then .
  else fail($label + " contains duplicate names")
  end;

def valid_slack_dm_allowlist:
  if type != "string" or length > 2048 then false
  elif . == "*" then true
  elif test("^U[A-Z0-9]{8,}(,U[A-Z0-9]{8,}){0,99}$") then
    split(",") as $ids |
    ($ids | length) == ($ids | unique | length)
  else false
  end;

def assert_current_contract:
  expected as $e |
  exactly_one_openclaw as $container |
  if ((keys - allowed_current_task_keys) | length) == 0
     then . else fail("current task definition has unexpected top-level fields") end |
  if (.enableFaultInjection // false) == false
     then . else fail("fault injection must be disabled") end |
  if .family != $e.family then fail("unexpected task family") else . end |
  if (.taskDefinitionArn // "") |
       test("^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$")
     then . else fail("current taskDefinitionArn is missing or belongs to another family") end |
  if .taskRoleArn != $e.taskRoleArn then fail("unexpected task role") else . end |
  if .executionRoleArn != $e.executionRoleArn then fail("unexpected execution role") else . end |
  if .networkMode != "awsvpc" then fail("networkMode must already be awsvpc") else . end |
  if (.requiresCompatibilities // []) == ["FARGATE"]
     then . else fail("requiresCompatibilities must be exactly FARGATE") end |
  if .runtimePlatform == {
       cpuArchitecture: "ARM64",
       operatingSystemFamily: "LINUX"
     }
     then . else fail("runtimePlatform must already be linux/arm64") end |
  if (.placementConstraints // []) == []
     then . else fail("placement constraints are forbidden") end |
  if ((.volumes // []) | valid_volumes)
     then . else fail("exact tmp and encrypted EFS state volumes are required") end |
  if (($container | keys) - allowed_container_keys | length) == 0
     then . else fail("openclaw container has unexpected fields") end |
  if ($container.mountPoints // []) == exact_mounts
     then . else fail("only exact tmp and EFS state mounts are approved") end |
  assert_unique_names(($container.environment // []); "environment") |
  if (($container.environment // [] | map(.name) | sort) ==
      ["AWS_REGION", "SLACK_DM_ALLOWLIST", "SLACK_TEAM_ID", "TMPDIR"])
     then . else fail("environment must contain exact runtime and Slack inputs") end |
  if ($container.environment // []) |
       all(
         (.name == "AWS_REGION" and .value == $e.region) or
         (.name == "TMPDIR" and .value == "/tmp") or
         (
           .name == "SLACK_TEAM_ID" and
           (.value | test("^T[A-Z0-9]{8,}$"))
         ) or
         (
           .name == "SLACK_DM_ALLOWLIST" and
           (.value | valid_slack_dm_allowlist)
         )
       )
     then . else fail("Slack team/DM environment contract is invalid") end |
  assert_unique_names(($container.secrets // []); "secrets") |
  if (($container.secrets // [] | map(.name) | sort) ==
      (required_secret_names | sort))
     then . else fail("secret names must be the exact approved set") end |
  if ($container.secrets // []) |
       all(
         .name as $name |
         (required_secret_patterns[$name] // "(?!)") as $pattern |
         ((keys - ["name", "valueFrom"]) | length) == 0 and
         (.valueFrom | test($pattern))
       )
     then . else fail("secret binding is not the approved Secrets Manager ARN") end |
  if $container.logConfiguration.logDriver == "awslogs" and
     $container.logConfiguration.options["awslogs-group"] == $e.logGroup and
     $container.logConfiguration.options["awslogs-region"] == $e.region and
     $container.logConfiguration.options["awslogs-stream-prefix"] == "openclaw" and
     (($container.logConfiguration | keys) - ["logDriver", "options"] | length) == 0 and
     (($container.logConfiguration.options | keys) -
       ["awslogs-group", "awslogs-region", "awslogs-stream-prefix"] | length) == 0
     then . else fail("unexpected log configuration") end;

def harden_openclaw_container($image):
  .image = $image |
  .essential = true |
  .readonlyRootFilesystem = true |
  .user = "65532:65532" |
  .privileged = false |
  del(.entryPoint, .command, .dockerSecurityOptions) |
  .linuxParameters = {
    initProcessEnabled: true,
    capabilities: {drop: ["ALL"]}
  } |
  .mountPoints = exact_mounts |
  .stopTimeout = 120 |
  .healthCheck = {
    command: [
      "CMD",
      "/usr/bin/node",
      "-e",
      "fetch(\"http://127.0.0.1:18789/readyz\").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
    ],
    interval: 30,
    timeout: 5,
    retries: 5,
    startPeriod: 40
  };

(.taskDefinition // .) |
expected as $e |
if ($image | test(
  ("^" + ($e.repository | gsub("\\."; "\\.")) + "@sha256:[0-9a-f]{64}$")
)) then . else
  fail("receipt image must be a digest in the fixed OpenClaw repository")
end |
assert_current_contract |
{
  family,
  taskRoleArn,
  executionRoleArn,
  networkMode,
  containerDefinitions,
  volumes,
  requiresCompatibilities,
  cpu,
  memory,
  runtimePlatform
} |
with_entries(select(.value != null)) |
.containerDefinitions = [
  (.containerDefinitions[0] | harden_openclaw_container($image))
] |
exactly_one_openclaw as $after |
if (
  .family == $e.family and
  .taskRoleArn == $e.taskRoleArn and
  .executionRoleArn == $e.executionRoleArn and
  .networkMode == "awsvpc" and
  .requiresCompatibilities == ["FARGATE"] and
  .runtimePlatform == {
    cpuArchitecture: "ARM64",
    operatingSystemFamily: "LINUX"
  } and
  (.volumes | valid_volumes) and
  ($after | keys - allowed_container_keys | length) == 0 and
  $after.readonlyRootFilesystem == true and
  $after.user == "65532:65532" and
  $after.privileged == false and
  $after.linuxParameters == {
    initProcessEnabled: true,
    capabilities: {drop: ["ALL"]}
  } and
  ($after | has("dockerSecurityOptions") | not) and
  ($after | has("entryPoint") | not) and
  ($after | has("command") | not) and
  $after.mountPoints == exact_mounts and
  $after.stopTimeout == 120 and
  $after.healthCheck.command[1] == "/usr/bin/node" and
  ($after.healthCheck.command[3] | contains("/readyz"))
) then . else
  fail("rendered hardening contract is incomplete")
end
