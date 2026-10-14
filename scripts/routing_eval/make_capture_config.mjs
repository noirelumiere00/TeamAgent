// 本番の openclaw.config.json5 から、雛形取り出し用の設定を作る（OpenClaw 公式イメージの中で動かす）。
//
// 変えるのは「外へ出る口」と秘密値だけで、モデル・ツール絞り・セッション・Slack の受信設定・
// bootstrap は本番のまま:
//   - Bedrock の baseUrl と MCP の url を、手元の代役サーバ（capture_stubs.py）へ向ける
//   - ${VAR} の秘密値参照をダミーへ置き換える（本物の秘密は使わない。Slack には接続しない）
//   - 本人特定 plugin（teamagent-caller-identity）を外す
//
// 外した本人特定 plugin は、プロンプトもツール定義も書き換えない（before_model_resolve は観測だけ）。
// ただし「連携」の短文を決定論で oauth_connect に回す層1・層2 はこの評価に乗らない（README に明記）。
//
// usage: node make_capture_config.mjs <src json5> <out json> <stub base url> <plugins dir>
//   <plugins dir> の下に slack/ と amazon-bedrock/ を置いておくこと。
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire("/app/package.json");
const JSON5 = require("json5");

const [srcPath, outPath, stubBase, pluginsDir] = process.argv.slice(2);
if (!srcPath || !outPath || !stubBase || !pluginsDir) {
  console.error("usage: node make_capture_config.mjs <src json5> <out json> <stub base url> <plugins dir>");
  process.exit(2);
}

const config = JSON5.parse(readFileSync(srcPath, "utf8"));

const slack = config.channels?.slack;
if (!slack) throw new Error("channels.slack が無い");
slack.botToken = "xoxb-routing-eval-dummy";
slack.appToken = "xapp-routing-eval-dummy";

config.plugins = {
  allow: ["slack", "amazon-bedrock"],
  load: { paths: [`${pluginsDir}/slack`, `${pluginsDir}/amazon-bedrock`] },
  entries: { slack: { enabled: true }, "amazon-bedrock": { enabled: true } },
};

const bedrock = config.models?.providers?.["amazon-bedrock"];
if (!bedrock) throw new Error("models.providers.amazon-bedrock が無い");
bedrock.baseUrl = stubBase;

const mcp = config.mcp?.servers?.teamagent;
if (!mcp) throw new Error("mcp.servers.teamagent が無い");
mcp.url = `${stubBase}/mcp`;
mcp.headers = { Authorization: "Bearer routing-eval-dummy" };

config.gateway.auth = { token: "routing-eval-dummy-gateway-token" };

const text = JSON.stringify(config, null, 2);
const leftover = text.match(/\$\{[A-Z0-9_]+\}/g);
if (leftover) throw new Error(`秘密値の参照が残っている: ${[...new Set(leftover)].join(", ")}`);
writeFileSync(outPath, `${text}\n`);
console.log(`capture config -> ${outPath}`);
