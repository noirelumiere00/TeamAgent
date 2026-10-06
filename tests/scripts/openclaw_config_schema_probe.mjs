// 本番 config（infra/openclaw/openclaw.config.json5）を、上流 openclaw の出荷物（dist）の zod schema で
// safeParse するプローブ（2026-09-30・DM の context overflow 対策で追加）。
//
// なぜ必要か:
//   OpenClaw は未知キー・範囲外の値を起動時の config validate で拒否し、コンテナは exit 78 で落ちる
//   （§S 実測）。CI には上流の dist が無いので、最終的な門は build 時の `openclaw config validate --json`
//   （build-image.sh）になる。便を撃つ前に手元で同じ判定を得るため、dist を渡したときだけ走らせる。
//
// 使い方: OPENCLAW_DIST_DIR=<openclaw@2026.7.1 の package/dist> node openclaw_config_schema_probe.mjs < config.json
//   入力は JSON（pytest 側が reviewed JSON5 パーサで読んだ実物）。eval はしない。
//   出力は {openclaw, slack, controls} の JSON。値は "OK" か、拒否の issue（先頭 3 件）。
//
// schema の場所（2026.7.1 の実物）:
//   zod-schema-O9ml_nmo.js の export `t` = OpenClawSchema（全体。channels.* は各チャンネルの schema に委ねる）
//   bundled-channel-config-schema-CkfMA6sO.js の export `s` = SlackConfigSchema
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const dist = process.env.OPENCLAW_DIST_DIR;
if (!dist) {
  process.stderr.write("OPENCLAW_DIST_DIR is required\n");
  process.exit(2);
}
const { t: OpenClawSchema } = await import(pathToFileURL(join(dist, "zod-schema-O9ml_nmo.js")).href);
const { s: SlackConfigSchema } = await import(
  pathToFileURL(join(dist, "bundled-channel-config-schema-CkfMA6sO.js")).href
);
if (typeof OpenClawSchema?.safeParse !== "function" || typeof SlackConfigSchema?.safeParse !== "function") {
  process.stderr.write("schema exports not found in OPENCLAW_DIST_DIR (upstream version drift?)\n");
  process.exit(3);
}

const config = JSON.parse(readFileSync(0, "utf8"));
const verdict = (schema, value) => {
  const parsed = schema.safeParse(value);
  return parsed.success
    ? "OK"
    : parsed.error.issues.slice(0, 3).map((i) => ({ code: i.code, path: i.path }));
};
const mutated = (mutate) => {
  const copy = structuredClone(config);
  mutate(copy);
  return copy;
};

// 対照: schema が「値まで」検査していることを毎回示す（全部が拒否されるべき）。
const controls = {
  dm_history_limit_negative: verdict(
    SlackConfigSchema,
    mutated((c) => {
      c.channels.slack.dmHistoryLimit = -1;
    }).channels.slack,
  ),
  tool_result_max_chars_string: verdict(
    OpenClawSchema,
    mutated((c) => {
      c.agents.defaults.contextLimits.toolResultMaxChars = "20000";
    }),
  ),
  reset_triggers_not_array: verdict(
    OpenClawSchema,
    mutated((c) => {
      c.session.resetTriggers = "新しい会話";
    }),
  ),
  reset_at_hour_out_of_range: verdict(
    OpenClawSchema,
    mutated((c) => {
      c.session.reset.atHour = 24;
    }),
  ),
  reset_mode_unknown: verdict(
    OpenClawSchema,
    mutated((c) => {
      c.session.reset.mode = "hourly";
    }),
  ),
  session_unknown_key: verdict(
    OpenClawSchema,
    mutated((c) => {
      c.session.notARealKey = true;
    }),
  ),
};

process.stdout.write(
  JSON.stringify(
    {
      openclaw: verdict(OpenClawSchema, config),
      slack: verdict(SlackConfigSchema, config.channels.slack),
      controls,
    },
    null,
    2,
  ) + "\n",
);
